"""Tests for ``imessage_chatdb.connection`` (DESIGN.md 8.4 connection).

Every database here is a synthetic file under ``tmp_path`` / ``tmp_path_factory``.
The session-scoped ``pristine_db`` fixture (``tests/conftest.py``) hashes its
file once the writer has closed and again at session teardown; any test in the
suite that opened it through the library and changed a byte fails the run.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from imessage_chatdb import connection as connection_mod
from imessage_chatdb.connection import (
    DEFAULT_CHATDB,
    open_connection,
    translate_sqlite_error,
    uri_for_path,
)
from imessage_chatdb.errors import (
    FULL_DISK_ACCESS_HINT,
    ChatDBAccessError,
    ChatDBBusy,
    ChatDBError,
)
from tests.conftest import FixtureDB, MakeDB, Pristine, assert_safe_db_path, sha256_of
from tests.fixtures import builders
from tests.fixtures.schema_profiles import create_schema

# ---------------------------------------------------------------------------
# basics
# ---------------------------------------------------------------------------


def test_default_path_constant() -> None:
    assert DEFAULT_CHATDB == "~/Library/Messages/chat.db"


def test_open_returns_row_autocommit_connection(pristine_db: Pristine) -> None:
    conn = open_connection(pristine_db.path)
    try:
        assert conn.row_factory is sqlite3.Row
        assert conn.isolation_level is None
        assert conn.in_transaction is False
        rows = conn.execute("SELECT count(*) AS n FROM message").fetchall()
        assert rows[0]["n"] == pristine_db.message_count
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        conn.close()


def test_accepts_str_and_pathlike(pristine_db: Pristine) -> None:
    for p in (pristine_db.path, str(pristine_db.path), os.fspath(pristine_db.path)):
        conn = open_connection(p)
        try:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
        finally:
            conn.close()


def test_relative_and_tilde_paths_are_absolutised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # expanduser + abspath: a "~"-relative path resolves under a *fake* HOME
    # (never the real ~/Library) and a bare filename resolves under cwd.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert connection_mod._abspath("~/x.db") == str(tmp_path / "x.db")
    assert connection_mod._abspath("y.db") == str(tmp_path / "y.db")
    assert uri_for_path("~/x.db").startswith("file:")


# ---------------------------------------------------------------------------
# read-only enforcement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("readonly_uri", [True, False])
def test_insert_raises_under_query_only(fixture_db: FixtureDB, readonly_uri: bool) -> None:
    conn = open_connection(fixture_db.path, readonly_uri=readonly_uri)
    try:
        with pytest.raises(sqlite3.OperationalError) as ei:
            conn.execute("INSERT INTO handle (id, service) VALUES (?, ?)", ("+15550009999", "SMS"))
        assert "readonly" in str(ei.value).lower()
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE handle SET service = 'SMS'")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("PRAGMA journal_mode=DELETE").fetchall()
    finally:
        conn.close()
    assert fixture_db.writer.execute("SELECT count(*) FROM handle").fetchone()[0] == 0


def test_pristine_insert_attempt_leaves_file_untouched(pristine_db: Pristine) -> None:
    conn = open_connection(pristine_db.path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO chat (guid) VALUES ('any;-;x')")
    finally:
        conn.close()
    assert sha256_of(pristine_db.path) == pristine_db.sha256


# ---------------------------------------------------------------------------
# URI quoting
# ---------------------------------------------------------------------------


def test_uri_quotes_space_and_percent(tmp_path: Path) -> None:
    p = tmp_path / "my chat 100%.db"
    uri = uri_for_path(p)
    assert uri.startswith("file:/") and uri.endswith("?mode=ro")
    assert "%20" in uri and "%25" in uri
    assert " " not in uri
    assert uri.count("?") == 1
    body = uri[len("file:") : -len("?mode=ro")]
    assert body.count("%25") == 1 and body.count("%20") == 2


def test_open_path_with_space_and_percent(pristine_db: Pristine) -> None:
    assert " " in pristine_db.path.name and "%" in pristine_db.path.name
    conn = open_connection(pristine_db.path)
    try:
        n = conn.execute("SELECT count(*) FROM message").fetchone()[0]
        assert n == pristine_db.message_count
    finally:
        conn.close()


def test_open_path_with_question_mark_and_hash(make_db: MakeDB) -> None:
    fx = make_db("macos26", name="odd?name#1 [x].db")
    cid = builders.add_chat(fx.writer, "any;-;+15550001234", 45)
    builders.add_message(fx.writer, cid, text="hi")
    conn = open_connection(fx.path)
    try:
        assert conn.execute("SELECT count(*) FROM message").fetchone()[0] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# readonly_uri=False compatibility shim
# ---------------------------------------------------------------------------


def test_readonly_uri_false_works(fixture_db: FixtureDB) -> None:
    cid = builders.add_chat(fixture_db.writer, "any;-;+15550001234", 45)
    builders.add_message(fixture_db.writer, cid, text="hi")
    conn = open_connection(fixture_db.path, readonly_uri=False)
    try:
        assert conn.row_factory is sqlite3.Row
        assert conn.isolation_level is None
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) AS n FROM message").fetchone()["n"] == 1
    finally:
        conn.close()


def test_readonly_uri_false_never_creates_a_file(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.db"
    with pytest.raises(ChatDBAccessError) as ei:
        open_connection(missing, readonly_uri=False)
    assert not missing.exists()
    assert not missing.with_name(missing.name + "-wal").exists()
    # ``?mode=rw`` (no CREATE): SQLite itself reports the missing file
    assert str(ei.value).startswith(f"{missing}: unable to open database file. ")
    assert isinstance(ei.value.__cause__, sqlite3.OperationalError)


def test_readonly_uri_false_opens_odd_filenames(make_db: MakeDB) -> None:
    """The shim now goes through a ``file:`` URI, so ``?``/``#``/``%``/space must be quoted."""
    fx = make_db("macos26", name="odd?name#2 [y] 100%.db")
    cid = builders.add_chat(fx.writer, "any;-;+15550001234", 45)
    builders.add_message(fx.writer, cid, text="hi")
    conn = open_connection(fx.path, readonly_uri=False)
    try:
        assert conn.execute("SELECT count(*) FROM message").fetchone()[0] == 1
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        conn.close()


def test_sqlite_is_the_first_thing_to_touch_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``os.path.exists`` / ``stat`` probe precedes ``sqlite3.connect`` in either mode.

    This is what lets the suite's ``sqlite3.connect`` guard refuse the real
    database before any filesystem access, whether or not that file exists.
    """
    seen: list[tuple[object, bool]] = []

    class Sentinel(Exception):
        pass

    def spy_connect(database: object, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        seen.append((database, bool(kwargs.get("uri"))))
        raise Sentinel

    def probed(*_: object, **__: object) -> bool:
        raise AssertionError("the path was probed before sqlite3.connect")

    class _NoProbePath:
        abspath = staticmethod(os.path.abspath)
        expanduser = staticmethod(os.path.expanduser)
        exists = isfile = lexists = isdir = staticmethod(probed)

    class _NoProbeOS:
        path = _NoProbePath
        fspath = staticmethod(os.fspath)
        PathLike = os.PathLike
        stat = lstat = access = staticmethod(probed)

    monkeypatch.setattr(sqlite3, "connect", spy_connect)
    monkeypatch.setattr(connection_mod, "os", _NoProbeOS)
    target = tmp_path / "never created.db"
    for readonly_uri, mode in ((True, "ro"), (False, "rw")):
        with pytest.raises(Sentinel):
            open_connection(target, readonly_uri=readonly_uri)
        assert seen[-1] == (f"file:{quote(str(target))}?mode={mode}", True)
    monkeypatch.undo()
    assert not target.exists()


# ---------------------------------------------------------------------------
# WAL visibility: mode=ro sees the WAL, immutable=1 does not
# ---------------------------------------------------------------------------


def test_wal_write_visible_to_mode_ro_but_not_immutable(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    w = fx.writer
    cid = builders.add_chat(w, "any;-;+15550001234", 45)
    builders.add_message(w, cid, text="checkpointed")
    # Fold everything so far into the main file; the next insert lives only in the WAL.
    w.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    builders.add_message(w, cid, text="wal only")
    wal = fx.path.with_name(fx.path.name + "-wal")
    assert wal.exists() and wal.stat().st_size > 0

    ro = open_connection(fx.path)
    try:
        assert ro.execute("SELECT count(*) FROM message").fetchone()[0] == 2
    finally:
        ro.close()

    # The forbidden mode, used here only to document why it is forbidden.
    imm = sqlite3.connect(f"file:{fx.path}?immutable=1", uri=True)
    try:
        assert imm.execute("SELECT count(*) FROM message").fetchone()[0] == 1
    finally:
        imm.close()

    # the library never emits immutable=1
    assert "immutable" not in uri_for_path(fx.path)


def test_fresh_connection_sees_later_writes(fixture_db: FixtureDB) -> None:
    cid = builders.add_chat(fixture_db.writer, "any;-;+15550001234", 45)
    conn = open_connection(fixture_db.path)
    try:
        assert conn.execute("SELECT count(*) FROM message").fetchone()[0] == 0
        builders.add_message(fixture_db.writer, cid, text="new")
        # autocommit reader holds no snapshot, so the write is visible on the next statement
        assert conn.execute("SELECT count(*) FROM message").fetchone()[0] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# busy / access errors
# ---------------------------------------------------------------------------


def test_begin_exclusive_on_rollback_journal_raises_busy(tmp_path: Path) -> None:
    path = assert_safe_db_path(tmp_path / "journal.db", tmp_path)
    writer = sqlite3.connect(path, isolation_level=None)
    try:
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        create_schema(writer, "macos27")
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("INSERT INTO handle (id, service) VALUES ('+15550001234', 'iMessage')")
        with pytest.raises(ChatDBBusy):
            open_connection(path, timeout=0.05)
        with pytest.raises(ChatDBBusy):
            open_connection(path, timeout=0.05, readonly_uri=False)
        writer.execute("ROLLBACK")
        conn = open_connection(path, timeout=0.05)
        try:
            assert conn.execute("SELECT count(*) FROM handle").fetchone()[0] == 0
        finally:
            conn.close()
    finally:
        writer.close()


def test_busy_is_a_chatdb_error_and_chains_cause(tmp_path: Path) -> None:
    path = assert_safe_db_path(tmp_path / "journal2.db", tmp_path)
    writer = sqlite3.connect(path, isolation_level=None)
    try:
        create_schema(writer, "macos14")
        writer.execute("BEGIN EXCLUSIVE")
        with pytest.raises(ChatDBError) as ei:
            open_connection(path, timeout=0.05)
        assert isinstance(ei.value, ChatDBBusy)
        assert isinstance(ei.value.__cause__, sqlite3.OperationalError)
        writer.execute("ROLLBACK")
    finally:
        writer.close()


@pytest.mark.parametrize("readonly_uri", [True, False])
def test_nonexistent_path_raises_access_error_naming_path_and_executable(
    tmp_path: Path, readonly_uri: bool
) -> None:
    missing = tmp_path / "missing" / "chat.db"
    with pytest.raises(ChatDBAccessError) as ei:
        open_connection(missing, readonly_uri=readonly_uri)
    msg = str(ei.value)
    assert msg.startswith(f"{missing}: ")
    assert sys.executable in msg
    assert "Full Disk Access" in msg
    assert ei.value.path == str(missing)
    assert ei.value.reason == "unable to open database file"
    assert ei.value.executable == sys.executable
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("readonly_uri", [True, False])
def test_access_error_has_the_same_shape_in_both_modes(tmp_path: Path, readonly_uri: bool) -> None:
    """``"<path>: <sqlite message>. <Full Disk Access advice>"`` -- identical in both modes."""
    missing = tmp_path / "shape" / "chat.db"
    with pytest.raises(ChatDBAccessError) as ei:
        open_connection(missing, readonly_uri=readonly_uri)
    advice = FULL_DISK_ACCESS_HINT.format(executable=sys.executable)
    assert str(ei.value) == f"{missing}: {ei.value.reason}. {advice}"
    assert "System Settings > Privacy & Security > Full Disk Access" in advice


def test_access_error_without_a_path_starts_at_the_reason() -> None:
    e = ChatDBAccessError("authorization denied")
    assert e.path is None
    assert str(e).startswith("authorization denied. ")
    translated = translate_sqlite_error(sqlite3.OperationalError("authorization denied"))
    assert str(translated) == str(e)


def test_not_a_database_raises_access_error(tmp_path: Path) -> None:
    junk = assert_safe_db_path(tmp_path / "junk.db", tmp_path)
    junk.write_bytes(b"this is not an sqlite file " * 64)
    for readonly_uri in (True, False):
        with pytest.raises(ChatDBAccessError) as ei:
            open_connection(junk, readonly_uri=readonly_uri)
        assert "not a database" in ei.value.reason
        assert str(ei.value).startswith(f"{junk}: ")
        assert ei.value.path == str(junk)
        assert sys.executable in str(ei.value)


def test_translate_sqlite_error_table() -> None:
    busy = translate_sqlite_error(sqlite3.OperationalError("database is locked"))
    assert isinstance(busy, ChatDBBusy)
    busy2 = translate_sqlite_error(sqlite3.OperationalError("database table is busy"))
    assert isinstance(busy2, ChatDBBusy)
    acc = translate_sqlite_error(sqlite3.OperationalError("unable to open database file"))
    assert isinstance(acc, ChatDBAccessError) and sys.executable in str(acc)
    acc2 = translate_sqlite_error(sqlite3.OperationalError("authorization denied"))
    assert isinstance(acc2, ChatDBAccessError)
    acc3 = translate_sqlite_error(sqlite3.DatabaseError("file is not a database"))
    assert isinstance(acc3, ChatDBAccessError)
    assert translate_sqlite_error(sqlite3.OperationalError("no such table: message")) is None
    assert translate_sqlite_error(sqlite3.ProgrammingError("bad")) is None


def test_other_operational_errors_propagate(tmp_path: Path) -> None:
    """An empty but valid SQLite file opens fine; unrelated errors are not wrapped."""
    path = assert_safe_db_path(tmp_path / "empty.db", tmp_path)
    sqlite3.connect(path).close()  # creates a zero-table database
    conn = open_connection(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("SELECT * FROM message").fetchall()
    finally:
        conn.close()


def test_ro_reader_does_not_block_writer(fixture_db: FixtureDB) -> None:
    """WAL: a library reader and the writer coexist (the relay's normal state)."""
    cid = builders.add_chat(fixture_db.writer, "any;-;+15550001234", 45)
    conn = open_connection(fixture_db.path)
    try:
        conn.execute("SELECT count(*) FROM message").fetchall()
        builders.add_message(fixture_db.writer, cid, text="while reader open")
        assert conn.execute("SELECT count(*) FROM message").fetchone()[0] == 1
    finally:
        conn.close()
