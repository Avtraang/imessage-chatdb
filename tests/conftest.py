"""Shared fixtures.

Every database a test touches is a synthetic file created under ``tmp_path``.
``assert_safe_db_path`` refuses anything under ``~/Library`` (where the real
``chat.db`` lives) and anything outside the test's ``tmp_path``; ``make_db``
calls it before creating a file.

That check is opt-in, so the autouse ``refuse_real_database`` fixture backs it
up for the whole session: ``sqlite3.connect`` is wrapped so that *any* open of
a file under ``~/Library`` - a plain path, a ``PathLike``, bytes, or the
``file:...?mode=ro`` URI that ``open_connection`` builds - raises
``UnsafeDatabasePath`` before SQLite ever sees the path.  Nothing in the
library or the tests can therefore reach the real database by accident, even a
test that calls ``open_connection()`` or ``sqlite3.connect()`` directly.

``Attachment.exists()`` expands ``attachment.filename`` and ``stat``s it, so
fixture rows may only carry synthetic filenames: ``assert_safe_attachment_filename``
refuses anything that starts with ``/Users/`` as written and anything under
``~/Library`` once ``~`` is expanded (where the real Messages attachments
live).  ``make_db`` runs it over every ``attachment`` row of every database it
created at teardown, and ``pristine_db`` before it seals its file.

``pristine_db`` is the suite-wide read-only guard (DESIGN.md 8.4 connection:
"fixture file sha256 unchanged after the whole suite"): one session-scoped WAL
database that the connection, facade, CLI and watch tests all open through the
library and never write to.  Its sha256 is taken once the writer has
checkpointed and closed and is re-checked at session teardown; a regression
anywhere in the read path that writes (a stray PRAGMA, a journal created by the
``readonly_uri=False`` shim, ...) fails the suite.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from tests.fixtures import builders
from tests.fixtures.schema_profiles import PROFILES, create_schema
from tests.fixtures.typedstream_writer import encode_attributed_body


class UnsafeDatabasePath(RuntimeError):
    """Raised when a test tries to use a database path that is not a tmp_path file."""


def _library_dir() -> Path:
    return (Path.home() / "Library").resolve()


def assert_safe_db_path(path: Path, tmp_path: Path) -> Path:
    """Return ``path`` resolved, or raise ``UnsafeDatabasePath``.

    The check is purely lexical (``Path.resolve`` + ``is_relative_to``); it never
    opens the file.
    """
    resolved = Path(path).expanduser().resolve()
    if resolved.is_relative_to(_library_dir()):
        raise UnsafeDatabasePath(f"refusing to touch a database under ~/Library: {resolved}")
    root = Path(tmp_path).resolve()
    if not resolved.is_relative_to(root):
        raise UnsafeDatabasePath(f"database path {resolved} is not under tmp_path {root}")
    return resolved


def connect_target(database: object) -> Path | None:
    """The file a ``sqlite3.connect`` ``database`` argument names, or ``None`` for in-memory.

    Understands plain paths (``str``, ``bytes``, ``os.PathLike``) and ``file:``
    URIs (``file:/abs/path?mode=ro`` as :func:`imessage_chatdb.open_connection`
    builds them, percent-encoding undone).  ``""``, ``:memory:`` and
    ``mode=memory`` URIs have no file.  Purely lexical: nothing is opened.
    """
    if isinstance(database, (bytes, os.PathLike)):
        text = os.fsdecode(database)
    elif isinstance(database, str):
        text = database
    else:
        return None
    if text.startswith("file:"):
        parts = urlsplit(text)
        if parse_qs(parts.query).get("mode") == ["memory"]:
            return None
        text = unquote(parts.path)
    if text == "" or text.startswith(":memory:"):
        return None
    return Path(text).expanduser().resolve()


def refuse_if_under_library(database: object) -> None:
    """Raise ``UnsafeDatabasePath`` when ``database`` names a file under ``~/Library``."""
    target = connect_target(database)
    if target is not None and target.is_relative_to(_library_dir()):
        raise UnsafeDatabasePath(f"refusing to open a database under ~/Library: {target}")


class UnsafeAttachmentFilename(RuntimeError):
    """A fixture ``attachment.filename`` that would let ``Attachment.exists()`` stat a real path."""


def assert_safe_attachment_filename(filename: str | None) -> None:
    """Refuse an ``attachment.filename`` that could point at a real file.

    Allowed: ``NULL``/``''``, ``~``-relative *synthetic* paths
    (``~/synthetic/...``), absolute paths under the test's ``tmp_path`` or an
    invented location such as ``/var/synthetic/...``.  Refused: anything that
    starts with ``/Users/`` as written, and anything under ``~/Library`` once
    ``~`` is expanded.  Purely lexical: nothing is opened or stat-ed here.
    """
    if not filename:
        return
    if filename.startswith("/Users/"):
        raise UnsafeAttachmentFilename(f"attachment.filename points into /Users/: {filename!r}")
    expanded = Path(os.path.normpath(os.path.expanduser(filename)))
    for library in (Path.home() / "Library", _library_dir()):
        if expanded.is_relative_to(library):
            raise UnsafeAttachmentFilename(
                f"attachment.filename expands under ~/Library: {filename!r} -> {expanded}"
            )


def assert_fixture_attachments_safe(conn: sqlite3.Connection) -> None:
    """Run :func:`assert_safe_attachment_filename` over every ``attachment`` row."""
    for (filename,) in conn.execute("SELECT filename FROM attachment").fetchall():
        assert_safe_attachment_filename(filename)


@pytest.fixture(scope="session", autouse=True)
def refuse_real_database() -> Iterator[Callable[..., sqlite3.Connection]]:
    """Wrap ``sqlite3.connect`` for the whole session so ``~/Library`` is unreachable.

    Session-scoped and autouse, so it is installed before any function-scoped
    fixture that wraps ``sqlite3.connect`` itself (``tests/test_db.py::traced``
    captures *this* wrapper as its ``real_connect`` and composes with it).
    Yields the guarded ``connect`` so a test can assert it is installed.
    """
    real_connect = sqlite3.connect

    def guarded_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        database = args[0] if args else kwargs.get("database")
        refuse_if_under_library(database)
        return real_connect(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sqlite3, "connect", guarded_connect)
        yield guarded_connect


@dataclass
class FixtureDB:
    """A synthetic WAL-mode database built from one schema profile.

    ``writer`` is an autocommit connection (``isolation_level=None``) so builder
    inserts are immediately visible to any reader.  ``db`` lazily constructs an
    ``imessage_chatdb.ChatDB`` for the file; unpacking ``writer, db = fx`` matches
    the design's ``(writer_conn, ChatDB)`` shape.
    """

    path: Path
    profile: str
    writer: sqlite3.Connection
    _db: Any = field(default=None, repr=False)

    @property
    def db(self) -> Any:
        if self._db is None:
            # Resolved at runtime (not a static import) so this file type-checks
            # and imports before the facade module (T8) exists.
            chatdb_cls = importlib.import_module("imessage_chatdb.db").ChatDB
            self._db = chatdb_cls(self.path)
        return self._db

    def __iter__(self) -> Iterator[Any]:
        yield self.writer
        yield self.db


MakeDB = Callable[..., FixtureDB]


@pytest.fixture
def safe_db_path(tmp_path: Path) -> Callable[[Path], Path]:
    """The path guard, bound to this test's ``tmp_path``."""

    def _check(path: Path) -> Path:
        return assert_safe_db_path(path, tmp_path)

    return _check


@pytest.fixture
def make_db(tmp_path: Path) -> Iterator[MakeDB]:
    """Factory: ``make_db(profile, name=None) -> FixtureDB`` under ``tmp_path``.

    Creates the file with ``PRAGMA journal_mode=WAL`` and the profile's DDL.
    At teardown every database's ``attachment.filename`` values are checked
    with :func:`assert_fixture_attachments_safe`, then the writer connections
    are closed.
    """
    opened: list[sqlite3.Connection] = []
    counter = 0

    def _make(profile: str = PROFILES[-1], name: str | None = None) -> FixtureDB:
        nonlocal counter
        if profile not in PROFILES:
            raise ValueError(f"unknown profile {profile!r}; expected one of {PROFILES}")
        counter += 1
        if name is None:
            name = f"chat-{profile}-{counter}.db"
        path = assert_safe_db_path(tmp_path / name, tmp_path)
        if path.exists():
            raise FileExistsError(path)
        writer = sqlite3.connect(path, isolation_level=None)
        opened.append(writer)
        mode = writer.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        assert str(mode).lower() == "wal", mode
        create_schema(writer, profile)
        return FixtureDB(path=path, profile=profile, writer=writer)

    yield _make

    try:
        for conn in opened:
            try:
                assert_fixture_attachments_safe(conn)
            except sqlite3.ProgrammingError:
                pass  # the test closed its writer itself; nothing left to inspect
    finally:
        for conn in opened:
            conn.close()


@pytest.fixture(params=PROFILES)
def profile(request: pytest.FixtureRequest) -> str:
    """Parametrises a test over all four schema profiles."""
    return str(request.param)


@pytest.fixture
def fixture_db(make_db: MakeDB, profile: str) -> FixtureDB:
    """A fresh database for the current ``profile``."""
    return make_db(profile)


# ---------------------------------------------------------------------------
# suite-wide sha256-guarded read-only database
# ---------------------------------------------------------------------------

PRISTINE_PHONE = "+15550001234"
PRISTINE_EMAIL = "test@example.invalid"
PRISTINE_CHAT = "any;-;+15550001234"
PRISTINE_GROUP = "any;+;chat5001"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class Pristine:
    """Handle on the session-wide read-only database (see ``pristine_db``)."""

    path: Path
    sha256: str
    message_count: int
    chat_guid: str
    group_guid: str
    chat_rowid: int
    group_rowid: int
    rowids: tuple[int, ...]  # ascending; the last one is the group's blob+attachment row
    attachment_guid: str


def _wal_is_empty(path: Path) -> bool:
    wal = path.with_name(path.name + "-wal")
    return not wal.exists() or wal.stat().st_size == 0


@pytest.fixture(scope="session")
def pristine_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Pristine]:
    """A populated ``macos27`` WAL database shared read-only by the whole suite.

    The writer checkpoints and closes before yielding so the main file is
    complete and the WAL is empty; at session teardown the main file's sha256
    must be unchanged and the WAL must still be absent or empty.  Tests may
    only ever *read* this database (through the library or plain sqlite3).
    """
    root = tmp_path_factory.mktemp("pristine")
    path = assert_safe_db_path(root / "chat with space 100%.db", root)
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("PRAGMA journal_mode=WAL").fetchall()
    create_schema(writer, "macos27")
    phone = builders.add_handle(writer, PRISTINE_PHONE)
    email = builders.add_handle(writer, PRISTINE_EMAIL)
    cid = builders.add_chat(writer, PRISTINE_CHAT, 45, PRISTINE_PHONE, handles=[phone])
    gid = builders.add_chat(
        writer, PRISTINE_GROUP, 43, "chat5001", display_name="Synthetic", handles=[phone, email]
    )
    rowids = [
        builders.add_message(writer, cid, text=f"synthetic {i}", handle=phone) for i in range(3)
    ]
    blob = builders.add_message(
        writer, gid, body=encode_attributed_body("synthetic blob"), handle=email, has_att=1
    )
    builders.add_attachment(writer, blob, "PRISTINE-ATT", "image/png", "p.png", "~/synthetic/p.png")
    rowids.append(blob)
    assert_fixture_attachments_safe(writer)
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    writer.close()
    assert _wal_is_empty(path)
    digest = sha256_of(path)
    yield Pristine(
        path=path,
        sha256=digest,
        message_count=len(rowids),
        chat_guid=PRISTINE_CHAT,
        group_guid=PRISTINE_GROUP,
        chat_rowid=cid,
        group_rowid=gid,
        rowids=tuple(rowids),
        attachment_guid="PRISTINE-ATT",
    )
    assert sha256_of(path) == digest, "a read-only test modified the shared fixture database"
    assert _wal_is_empty(path), "a read-only test wrote to the shared fixture's WAL"
