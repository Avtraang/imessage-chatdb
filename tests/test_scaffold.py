"""Smoke tests for the T0 scaffold: packaging, errors, fixtures, path guard."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

import imessage_chatdb
from imessage_chatdb import errors
from tests.conftest import (
    FixtureDB,
    MakeDB,
    UnsafeAttachmentFilename,
    UnsafeDatabasePath,
    assert_fixture_attachments_safe,
    assert_safe_attachment_filename,
    assert_safe_db_path,
    connect_target,
)
from tests.fixtures import builders
from tests.fixtures.schema_profiles import (
    PROFILES,
    TABLES,
    column_names,
    missing_columns,
    table_columns,
)

# ---------- package ----------


def test_version_and_all() -> None:
    assert imessage_chatdb.__version__ == "0.2.0"
    assert "ChatDB" in imessage_chatdb.__all__
    # ``open`` is a package attribute but deliberately not an export (``import *`` safety)
    assert "open" not in imessage_chatdb.__all__
    assert callable(imessage_chatdb.open)
    assert imessage_chatdb.open.__module__ == "imessage_chatdb.db"
    assert "__version__" in imessage_chatdb.__all__
    assert len(imessage_chatdb.__all__) == len(set(imessage_chatdb.__all__))


def test_lazy_export_resolves_existing_module() -> None:
    assert imessage_chatdb.ChatDBError is errors.ChatDBError
    assert imessage_chatdb.SchemaError is errors.SchemaError


def test_lazy_export_unknown_name() -> None:
    with pytest.raises(AttributeError):
        _ = imessage_chatdb.definitely_not_a_public_name


def test_py_typed_ships() -> None:
    assert (Path(imessage_chatdb.__file__).parent / "py.typed").exists()


# ---------- errors ----------


def test_error_hierarchy() -> None:
    for cls in (
        errors.ChatDBAccessError,
        errors.ChatDBBusy,
        errors.CursorAhead,
        errors.SchemaError,
    ):
        assert issubclass(cls, errors.ChatDBError)
    assert issubclass(errors.ChatDBError, Exception)


def test_cursor_ahead_carries_values() -> None:
    e = errors.CursorAhead(500, 100)
    assert (e.cursor_rowid, e.max_rowid) == (500, 100)
    assert "500" in str(e) and "100" in str(e)


def test_schema_error_names_column() -> None:
    e = errors.SchemaError("message.guid")
    assert e.column == "message.guid"
    assert "message.guid" in str(e)


def test_access_error_names_executable_and_pane() -> None:
    import sys

    e = errors.ChatDBAccessError("unable to open database file")
    assert sys.executable in str(e)
    assert "Full Disk Access" in str(e)


# ---------- schema profiles ----------


def test_profiles_are_cumulative() -> None:
    assert PROFILES == ("macos14", "macos15", "macos26", "macos27")
    assert missing_columns("macos27") == frozenset()
    assert "message.thread_originator_part" in missing_columns("macos14")
    assert "message.thread_originator_part" not in missing_columns("macos15")
    assert "message.associated_message_emoji" in missing_columns("macos15")
    assert "chat.is_filtered" in missing_columns("macos15")
    assert "message.date_updated" in missing_columns("macos26")
    assert "message.group_title" in missing_columns("macos26")


def test_required_columns_present_on_every_profile(profile: str) -> None:
    cols = column_names(profile)
    assert set(cols["message"]) >= {
        "ROWID",
        "guid",
        "text",
        "attributedBody",
        "date",
        "is_from_me",
        "handle_id",
        "cache_has_attachments",
        "associated_message_guid",
        "associated_message_type",
    }
    assert set(cols["chat"]) >= {"ROWID", "guid", "style", "chat_identifier", "display_name"}
    assert set(cols["handle"]) >= {"ROWID", "id"}
    assert set(cols["attachment"]) >= {"ROWID", "guid", "filename", "mime_type", "transfer_name"}


def test_ddl_matches_declared_columns(fixture_db: FixtureDB) -> None:
    introspected = table_columns(fixture_db.writer)
    assert set(introspected) == set(TABLES)
    assert introspected == column_names(fixture_db.profile)


# ---------- fixtures / builders ----------


def test_make_db_is_wal_under_tmp_path(fixture_db: FixtureDB, tmp_path: Path) -> None:
    assert fixture_db.path.is_relative_to(tmp_path.resolve())
    mode = fixture_db.writer.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    # a second, independent reader sees the same mode
    reader = sqlite3.connect(fixture_db.path)
    try:
        assert reader.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        reader.close()


def test_build_chat_message_attachment(fixture_db: FixtureDB) -> None:
    w = fixture_db.writer
    hid = builders.add_handle(w, "+15550001234", "iMessage")
    cid = builders.add_chat(w, "any;-;+15550001234", 45, "+15550001234", handles=[hid])
    mid = builders.add_message(w, cid, text="hello", handle=hid, has_att=1)
    aid = builders.add_attachment(
        w, mid, "ATT-1", "image/png", "IMG_0001.png", "~/Attachments/synthetic/IMG_0001.png"
    )

    assert w.execute("SELECT count(*) FROM chat_handle_join").fetchone()[0] == 1
    assert w.execute(
        "SELECT chat_id, message_id FROM chat_message_join"
    ).fetchall() == [(cid, mid)]
    assert w.execute(
        "SELECT message_id, attachment_id FROM message_attachment_join"
    ).fetchall() == [(mid, aid)]
    row = w.execute(
        "SELECT text, handle_id, cache_has_attachments, date, guid FROM message WHERE ROWID = ?",
        (mid,),
    ).fetchone()
    assert row[0] == "hello"
    assert row[1] == hid
    assert row[2] == 1
    assert row[3] == builders.BASE_DATE_NS
    assert row[4] == "SYN-MSG-000001"

    # a second reader connection sees the WAL writes without an explicit commit
    reader = sqlite3.connect(fixture_db.path)
    try:
        assert reader.execute("SELECT count(*) FROM message").fetchone()[0] == 1
    finally:
        reader.close()


def test_builders_drop_absent_columns(fixture_db: FixtureDB) -> None:
    w = fixture_db.writer
    cid = builders.add_chat(w, "any;+;chat1", 43, "chat1", display_name="Group")
    # emoji / group_title are absent on older profiles; the builder must not raise
    mid = builders.add_message(
        w, cid, text="x", assoc_type=2006, assoc_emoji="\U0001f600", group_title="t"
    )
    if builders.has_column(w, "message", "associated_message_emoji"):
        got = w.execute(
            "SELECT associated_message_emoji FROM message WHERE ROWID = ?", (mid,)
        ).fetchone()[0]
        assert got == "\U0001f600"


def test_orphan_message_when_join_false(fixture_db: FixtureDB) -> None:
    w = fixture_db.writer
    cid = builders.add_chat(w, "any;-;test@example.invalid", 45, "test@example.invalid")
    builders.add_message(w, cid, text="joined")
    builders.add_message(w, cid, text="orphan", join=False)
    assert w.execute("SELECT count(*) FROM message").fetchone()[0] == 2
    assert w.execute("SELECT count(*) FROM chat_message_join").fetchone()[0] == 1


def test_auto_dates_ascend(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    cid = builders.add_chat(fx.writer, "any;-;+15550001234", 45)
    a = builders.add_message(fx.writer, cid, text="a")
    b = builders.add_message(fx.writer, cid, text="b")
    da, db_ = (
        fx.writer.execute("SELECT date FROM message WHERE ROWID = ?", (r,)).fetchone()[0]
        for r in (a, b)
    )
    assert db_ - da == builders.DATE_STEP_NS


def test_guid_uniqueness_enforced(make_db: MakeDB) -> None:
    fx = make_db("macos26")
    cid = builders.add_chat(fx.writer, "any;-;+15550001234", 45)
    builders.add_message(fx.writer, cid, guid="DUP")
    with pytest.raises(sqlite3.IntegrityError):
        builders.add_message(fx.writer, cid, guid="DUP")


def test_make_db_rejects_unknown_profile(make_db: MakeDB) -> None:
    with pytest.raises(ValueError):
        make_db("macos13")


# ---------- path guard ----------


def test_conftest_refuses_home_library(tmp_path: Path) -> None:
    real = Path.home() / "Library" / "Messages" / "chat.db"
    with pytest.raises(UnsafeDatabasePath):
        assert_safe_db_path(real, tmp_path)
    with pytest.raises(UnsafeDatabasePath):
        assert_safe_db_path(Path("~/Library/Messages/chat.db"), tmp_path)


def test_conftest_refuses_paths_outside_tmp_path(tmp_path: Path) -> None:
    with pytest.raises(UnsafeDatabasePath):
        assert_safe_db_path(Path("/nonexistent/elsewhere/chat.db"), tmp_path)


def test_conftest_accepts_tmp_path(tmp_path: Path) -> None:
    p = assert_safe_db_path(tmp_path / "ok.db", tmp_path)
    assert p.is_relative_to(tmp_path.resolve())


# ---------- attachment filename guard (Attachment.exists() must never stat a real path) ----------


def test_attachment_filename_guard_rejects_real_locations(tmp_path: Path) -> None:
    under_home_library = str(Path.home() / "Library" / "Messages" / "Attachments" / "x.png")
    for bad in (
        "~/Library/Messages/Attachments/ab/cd/x.png",
        under_home_library,
        "/Users/someone/Pictures/x.png",
    ):
        with pytest.raises(UnsafeAttachmentFilename):
            assert_safe_attachment_filename(bad)
    for ok in (None, "", "~/synthetic/x.png", "/var/synthetic/a.png", str(tmp_path / "x.png")):
        assert_safe_attachment_filename(ok)


def test_fixture_attachment_filenames_are_checked(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    cid = builders.add_chat(fx.writer, "any;-;+15550001234", 45)
    mid = builders.add_message(fx.writer, cid, text="x", has_att=1)
    builders.add_attachment(fx.writer, mid, "OK", "image/png", "ok.png", "~/synthetic/ok.png")
    builders.add_attachment(fx.writer, mid, "NULL", "image/png", "n.png", None)
    assert_fixture_attachments_safe(fx.writer)
    builders.add_attachment(
        fx.writer, mid, "BAD", "image/png", "bad.png", "~/Library/Messages/Attachments/bad.png"
    )
    with pytest.raises(UnsafeAttachmentFilename):
        assert_fixture_attachments_safe(fx.writer)
    # leave the fixture clean: make_db runs the same check again at teardown
    fx.writer.execute("DELETE FROM attachment WHERE guid = 'BAD'")
    assert_fixture_attachments_safe(fx.writer)


# ---------- suite-wide ~/Library guard (autouse, DESIGN.md section 8) ----------


REAL_CHATDB = Path.home() / "Library" / "Messages" / "chat.db"


def test_sqlite_connect_is_guarded_for_the_whole_session(
    refuse_real_database: object,
) -> None:
    assert sqlite3.connect is refuse_real_database


@pytest.mark.parametrize(
    "database",
    [
        str(REAL_CHATDB),
        REAL_CHATDB,
        "~/Library/Messages/chat.db",
        str(REAL_CHATDB).encode(),
        f"file:{REAL_CHATDB}?mode=ro",
        "file:" + str(REAL_CHATDB).replace(" ", "%20") + "?mode=ro&immutable=1",
    ],
    ids=["str", "path", "tilde", "bytes", "uri", "uri-immutable"],
)
def test_sqlite_connect_refuses_the_real_database_lexically(database: object) -> None:
    """The guard fires before SQLite sees the path, so nothing is ever opened."""
    uri = isinstance(database, str) and database.startswith("file:")
    with pytest.raises(UnsafeDatabasePath):
        sqlite3.connect(database, uri=uri)
    with pytest.raises(UnsafeDatabasePath):
        sqlite3.connect(database=database, uri=uri)


def test_library_entry_points_cannot_reach_the_real_database() -> None:
    from imessage_chatdb import ChatDB, open_connection
    from imessage_chatdb import open as open_db

    with pytest.raises(UnsafeDatabasePath):
        open_connection(REAL_CHATDB)
    with pytest.raises(UnsafeDatabasePath):
        open_connection(REAL_CHATDB, readonly_uri=False)
    with pytest.raises(UnsafeDatabasePath):
        open_db(REAL_CHATDB)
    with pytest.raises(UnsafeDatabasePath):
        ChatDB(REAL_CHATDB).max_rowid()
    with pytest.raises(UnsafeDatabasePath):
        ChatDB().initial_cursor()  # the default path is the real database


def test_guard_wins_even_when_the_real_database_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux CI, or a Mac with no Messages data: ``~/Library/Messages/chat.db`` is absent.

    The library never probes the path before ``sqlite3.connect`` (not even in
    the ``readonly_uri=False`` shim, which used to ``os.path.exists`` first), so
    the guard still fires in both modes and nothing is created.
    """
    from imessage_chatdb import ChatDB, open_connection

    monkeypatch.setenv("HOME", str(tmp_path))
    missing = Path("~/Library/Messages/chat.db").expanduser()
    assert missing.is_relative_to(tmp_path) and not missing.exists()
    for readonly_uri in (True, False):
        with pytest.raises(UnsafeDatabasePath):
            open_connection(missing, readonly_uri=readonly_uri)
    with pytest.raises(UnsafeDatabasePath):
        ChatDB().initial_cursor()  # the default path, now under the fake HOME
    assert not missing.exists() and not missing.parent.exists()


def test_guard_lets_tmp_path_and_in_memory_databases_through(
    tmp_path: Path, safe_db_path: Callable[[Path], Path]
) -> None:
    from imessage_chatdb.connection import uri_for_path

    path = safe_db_path(tmp_path / "ok with space 100%.db")
    for database, uri in [
        (path, False),
        (str(path), False),
        (uri_for_path(path), True),
        (":memory:", False),
        ("file::memory:?cache=shared", True),
        ("file:guard-mem?mode=memory", True),
    ]:
        conn = sqlite3.connect(database, uri=uri)
        try:
            assert conn.execute("SELECT 1").fetchone() == (1,)
        finally:
            conn.close()


def test_connect_target_parsing() -> None:
    assert connect_target(":memory:") is None
    assert connect_target("") is None
    assert connect_target("file::memory:") is None
    assert connect_target("file:x?mode=memory") is None
    assert connect_target(123) is None
    assert connect_target("file:/a/b%20c?mode=ro") == Path("/a/b c").resolve()
    assert connect_target(b"/a/b") == Path("/a/b").resolve()
    assert connect_target("~/x.db") == (Path.home() / "x.db").resolve()
