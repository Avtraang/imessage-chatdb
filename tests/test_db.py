"""Tests for the ``ChatDB`` facade, ``open()`` and the package exports (T8).

Connections are traced by swapping ``sqlite3.connect`` for one that builds a
``sqlite3.Connection`` subclass which records every open and close, so each
test can state exactly how many connections a call used and that each was
closed in ``finally``.  Synthetic databases only.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import imessage_chatdb
from imessage_chatdb import attachments as att_mod
from imessage_chatdb import chats as chats_mod
from imessage_chatdb import messages as msg_mod
from imessage_chatdb import search as search_mod
from imessage_chatdb.connection import open_connection
from imessage_chatdb.db import ChatDB, open
from imessage_chatdb.errors import ChatDBAccessError, ChatDBBusy
from imessage_chatdb.link_preview import LINK_BALLOON, embedded_image
from imessage_chatdb.models import ChatActivity
from imessage_chatdb.polling import Cursor
from imessage_chatdb.schema import Schema
from tests.conftest import FixtureDB, MakeDB, Pristine, sha256_of
from tests.fixtures import builders as b
from tests.fixtures.keyed_archive_writer import make_link_payload
from tests.fixtures.typedstream_writer import encode_attributed_body

PHONE = "+15550001234"
EMAIL = "test@example.invalid"
CHAT = "any;-;+15550001234"
GROUP = "any;+;chat7001"


# ---------------------------------------------------------------------------
# connection tracing
# ---------------------------------------------------------------------------


class TracedConnection(sqlite3.Connection):
    """A connection that remembers whether ``close()`` was called."""

    closed: bool

    def close(self) -> None:
        self.closed = True
        super().close()


@pytest.fixture
def traced(monkeypatch: pytest.MonkeyPatch) -> list[TracedConnection]:
    """Swap ``sqlite3.connect`` so every connection opened from now on is recorded."""
    opened: list[TracedConnection] = []
    real_connect = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs.setdefault("factory", TracedConnection)
        conn = real_connect(*args, **kwargs)
        assert isinstance(conn, TracedConnection)
        conn.closed = False
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    return opened


def _all_closed(opened: list[TracedConnection]) -> bool:
    return all(c.closed for c in opened)


@pytest.fixture
def populated(fixture_db: FixtureDB) -> dict[str, Any]:
    """A small but complete database: 1:1 chat, group, attachments, reply, link, edit."""
    w = fixture_db.writer
    one = b.add_chat(w, CHAT, handles=[PHONE])
    group = b.add_chat(w, GROUP, 43, "chat7001", display_name="Synthetic", handles=[PHONE, EMAIL])
    m1 = b.add_message(w, one, text="hello there", handle=PHONE)
    m2 = b.add_message(w, one, body=encode_attributed_body("blob only"), is_from_me=1)
    m3 = b.add_message(w, one, text=None, handle=PHONE, has_att=1)
    b.add_attachment(w, m3, "ATT-1", "image/png", "IMG_0001.png", "~/Attachments/syn/IMG_0001.png")
    b.add_attachment(w, m3, "ATT-2", None, "x.pluginPayloadAttachment", None)
    m4 = b.add_message(w, group, text="reply", handle=EMAIL, reply_to="SYN-MSG-000001")
    m5 = b.add_message(
        w,
        group,
        text="https://example.invalid/page",
        handle=PHONE,
        balloon=LINK_BALLOON,
        payload=make_link_payload("https://example.invalid/page", "Title", embed="png"),
    )
    m6 = b.add_message(w, group, text="edited later", is_from_me=1, date_edited=12345)
    return {
        "fx": fixture_db,
        "one": one,
        "group": group,
        "msgs": [m1, m2, m3, m4, m5, m6],
    }


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------


def test_init_holds_no_connection(tmp_path: Path, traced: list[TracedConnection]) -> None:
    db = ChatDB(tmp_path / "never-opened.db")
    assert traced == []
    assert db.path == tmp_path / "never-opened.db"
    assert db.timeout == 5.0 and db.readonly_uri is True
    assert repr(db) == f"ChatDB({str(tmp_path / 'never-opened.db')!r})"
    db.close()  # no-op
    assert traced == []


def test_path_is_expanded_and_pathlike() -> None:
    db = ChatDB("~/synthetic-never-opened.db")
    assert db.path == Path.home() / "synthetic-never-opened.db"
    assert isinstance(db.path, Path)
    assert ChatDB(Path("/tmp/x.db")).path == Path("/tmp/x.db")


def test_default_path_is_chat_db() -> None:
    db = ChatDB()
    assert db.path == Path("~/Library/Messages/chat.db").expanduser()
    assert imessage_chatdb.DEFAULT_CHATDB == "~/Library/Messages/chat.db"


def test_options_forwarded_to_open_connection(
    fixture_db: FixtureDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[Any, ...]] = []

    def spy(path: Any, *, timeout: float, readonly_uri: bool) -> sqlite3.Connection:
        seen.append((Path(path), timeout, readonly_uri))
        return open_connection(path, timeout=timeout, readonly_uri=readonly_uri)

    monkeypatch.setattr("imessage_chatdb.db.open_connection", spy)
    db = ChatDB(fixture_db.path, timeout=1.5, readonly_uri=False)
    db.max_rowid()
    assert seen == [(fixture_db.path, 1.5, False)]


# ---------------------------------------------------------------------------
# one connection per call
# ---------------------------------------------------------------------------


def test_connect_returns_a_fresh_read_only_connection_each_call(
    fixture_db: FixtureDB, traced: list[TracedConnection]
) -> None:
    db = fixture_db.db
    c1 = db.connect()
    c2 = db.connect()
    try:
        assert c1 is not c2
        assert len(traced) == 2 and not any(c.closed for c in traced)
        assert c1.row_factory is sqlite3.Row
        assert c1.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            c1.execute("INSERT INTO handle (id) VALUES ('+15550009999')")
    finally:
        c1.close()
        c2.close()
    assert _all_closed(traced)


def _facade_calls(db: ChatDB, d: dict[str, Any]) -> list[tuple[str, Callable[[], Any]]]:
    one, group, msgs = d["one"], d["group"], d["msgs"]
    return [
        ("max_rowid", db.max_rowid),
        ("max_date_edited", db.max_date_edited),
        ("initial_cursor", db.initial_cursor),
        ("messages_after", lambda: db.messages_after(0)),
        ("messages_edited_after", lambda: db.messages_edited_after(0)),
        ("thread_messages", lambda: db.thread_messages(CHAT)),
        ("recent_messages", lambda: db.recent_messages(GROUP)),
        ("message", lambda: db.message(msgs[2])),
        ("messages_by_guid", lambda: db.messages_by_guid(["SYN-MSG-000001"])),
        ("reply_targets", lambda: db.reply_targets(["SYN-MSG-000001"])),
        ("attachments_for", lambda: db.attachments_for([msgs[2]])),
        ("attachment", lambda: db.attachment("ATT-1")),
        ("chat_attachments", lambda: db.chat_attachments(CHAT)),
        ("payload", lambda: db.payload(msgs[4])),
        ("link_image", lambda: db.link_image(msgs[4])),
        ("chats", db.chats),
        ("chat", lambda: db.chat(GROUP)),
        ("chat_by_rowid", lambda: db.chat_by_rowid(group)),
        ("participants", lambda: db.participants(group)),
        ("participants_map", db.participants_map),
        ("chat_services", lambda: db.chat_services([one, group])),
        ("last_rowid_for", lambda: db.last_rowid_for(one)),
        ("chats_changed_since", lambda: db.chats_changed_since(0)),
        ("unread_count", lambda: db.unread_count(one, 0)),
        ("lite_messages", lambda: db.lite_messages(msgs)),
        ("one_to_one_activity", db.one_to_one_activity),
        ("find_chat", lambda: db.find_chat([PHONE])),
        ("search", lambda: db.search("hello")),
        ("poll", lambda: db.poll(Cursor())),
    ]


def test_every_facade_method_opens_and_closes_exactly_one_connection(
    populated: dict[str, Any], traced: list[TracedConnection]
) -> None:
    fx: FixtureDB = populated["fx"]
    db = ChatDB(fx.path)  # fresh: the schema is not yet introspected
    assert traced == []
    for i, (name, call) in enumerate(_facade_calls(db, populated), start=1):
        call()
        assert len(traced) == i, f"{name} opened {len(traced) - i + 1} connections"
        assert traced[-1].closed, f"{name} left its connection open"
        with pytest.raises(sqlite3.ProgrammingError):
            traced[-1].execute("SELECT 1")


def test_connection_closed_even_when_the_call_raises(
    populated: dict[str, Any], traced: list[TracedConnection]
) -> None:
    fx: FixtureDB = populated["fx"]
    db = ChatDB(fx.path)
    fx.writer.execute("DROP TABLE message_attachment_join")
    with pytest.raises(sqlite3.OperationalError):
        db.attachments_for(populated["msgs"])
    assert len(traced) == 1 and traced[0].closed


def test_schema_is_introspected_once_lazily(
    populated: dict[str, Any], traced: list[TracedConnection]
) -> None:
    fx: FixtureDB = populated["fx"]
    db = ChatDB(fx.path)
    stmts: list[str] = []
    # the first call that needs the schema introspects it on *that* call's connection
    db.max_rowid()  # does not need the schema
    assert len(traced) == 1
    s1 = db.schema
    assert isinstance(s1, Schema) and s1.profile == fx.profile
    assert len(traced) == 2 and traced[-1].closed
    assert db.schema is s1  # cached
    assert len(traced) == 2
    # later calls never re-run PRAGMA table_info
    with db.connection() as conn:
        conn.set_trace_callback(stmts.append)
        db.messages_after(0)
        db.thread_messages(CHAT)
        conn.set_trace_callback(None)
    assert not any("table_info" in s for s in stmts)
    # refresh_schema rebuilds on one connection and replaces the cache
    s2 = db.refresh_schema()
    assert s2 is not s1 and db.schema is s2
    assert s2.columns == s1.columns
    assert _all_closed(traced)


def test_first_query_call_introspects_schema_on_its_own_connection(
    populated: dict[str, Any], traced: list[TracedConnection]
) -> None:
    fx: FixtureDB = populated["fx"]
    db = ChatDB(fx.path)
    stmts: list[str] = []
    real = ChatDB.connect

    def tracing_connect(self: ChatDB) -> sqlite3.Connection:
        conn = real(self)
        conn.set_trace_callback(stmts.append)
        return conn

    ChatDB.connect = tracing_connect  # type: ignore[method-assign]
    try:
        msgs = db.messages_after(0)
    finally:
        ChatDB.connect = real  # type: ignore[method-assign]
    assert len(msgs) == 6
    assert len(traced) == 1 and traced[0].closed
    assert sum("table_info" in s for s in stmts) == 7  # one PRAGMA per table, once


# ---------------------------------------------------------------------------
# connection() batching
# ---------------------------------------------------------------------------


def test_connection_batches_calls_on_one_connection(
    populated: dict[str, Any], traced: list[TracedConnection]
) -> None:
    fx: FixtureDB = populated["fx"]
    db = ChatDB(fx.path)
    with db.connection() as conn:
        assert isinstance(conn, sqlite3.Connection)
        assert len(traced) == 1 and traced[0] is conn
        for _name, call in _facade_calls(db, populated):
            call()
        assert len(traced) == 1
        assert not conn.closed
        # nesting reuses the same connection
        with db.connection() as inner:
            assert inner is conn
            db.max_rowid()
        assert len(traced) == 1
        assert not conn.closed  # inner exit does not close
    assert traced[0].closed
    # outside the block, calls open their own connections again
    db.max_rowid()
    assert len(traced) == 2 and traced[1].closed


def test_connection_closes_on_exception_and_resets_batch(
    populated: dict[str, Any], traced: list[TracedConnection]
) -> None:
    fx: FixtureDB = populated["fx"]
    db = ChatDB(fx.path)
    with pytest.raises(RuntimeError):
        with db.connection():
            raise RuntimeError("boom")
    assert len(traced) == 1 and traced[0].closed
    db.max_rowid()
    assert len(traced) == 2  # a new one, not the closed batch connection


def test_connection_gives_consistent_cursor_reads(populated: dict[str, Any]) -> None:
    fx: FixtureDB = populated["fx"]
    db = fx.db
    with db.connection():
        top = db.max_rowid()
        cur = db.initial_cursor()
    assert cur == Cursor(rowid=top, edit_mark=db.max_date_edited())
    assert cur.edit_mark == (12345 if db.schema.has("message", "date_edited") else 0)


def test_connection_translates_lock_to_busy(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    w = fx.writer
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    b.add_message(w, chat, text="x")
    assert w.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    db = ChatDB(fx.path, timeout=0.05)
    # lock held before the open: raised by open_connection's probe
    w.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(ChatDBBusy):
            db.max_rowid()
    finally:
        w.execute("COMMIT")
    # lock taken while a batch connection is open: raised from the query, translated
    with pytest.raises(ChatDBBusy) as info:
        with db.connection():
            assert db.max_rowid() == 1
            w.execute("BEGIN EXCLUSIVE")
            try:
                db.max_rowid()
            finally:
                w.execute("COMMIT")
    assert isinstance(info.value.__cause__, sqlite3.OperationalError)
    assert db.max_rowid() == 1  # usable again


def test_connection_does_not_translate_other_sqlite_errors(populated: dict[str, Any]) -> None:
    fx: FixtureDB = populated["fx"]
    with pytest.raises(sqlite3.OperationalError) as info:
        with fx.db.connection() as conn:
            conn.execute("INSERT INTO handle (id) VALUES ('+15550009999')")
    assert not isinstance(info.value, ChatDBBusy)


# ---------------------------------------------------------------------------
# facade methods delegate with the documented defaults
# ---------------------------------------------------------------------------


@pytest.fixture
def reader(populated: dict[str, Any]) -> Iterator[tuple[sqlite3.Connection, Schema]]:
    conn = open_connection(populated["fx"].path)
    try:
        yield conn, Schema(conn)
    finally:
        conn.close()


def test_message_methods_match_module_functions(
    populated: dict[str, Any], reader: tuple[sqlite3.Connection, Schema]
) -> None:
    db: ChatDB = populated["fx"].db
    conn, schema = reader
    msgs = populated["msgs"]
    assert db.max_rowid() == msg_mod.max_rowid(conn) == msgs[-1]
    assert db.max_date_edited() == msg_mod.max_date_edited(conn, schema)
    assert db.initial_cursor() == Cursor(
        msg_mod.max_rowid(conn), msg_mod.max_date_edited(conn, schema)
    )

    assert db.messages_after(0) == msg_mod.messages_after(conn, schema, 0)
    assert [m.rowid for m in db.messages_after(msgs[1], limit=2)] == msgs[2:4]
    assert all(m.enriched for m in db.messages_after(0))
    assert not any(m.enriched for m in db.messages_after(0, enrich=False))
    orphan = b.add_message(populated["fx"].writer, populated["one"], text="orphan", join=False)
    assert orphan not in {m.rowid for m in db.messages_after(0)}
    assert orphan in {m.rowid for m in db.messages_after(0, include_orphans=True)}

    assert db.messages_edited_after(0) == msg_mod.messages_edited_after(conn, schema, 0)
    edited, mark = db.messages_edited_after(0)
    if schema.has("message", "date_edited"):
        assert [m.rowid for m in edited] == [msgs[5]] and mark == 12345
    else:
        assert (edited, mark) == ([], 0)

    assert db.thread_messages(CHAT) == msg_mod.thread_messages(conn, schema, CHAT)
    assert [m.rowid for m in db.thread_messages(CHAT)] == msgs[:3]  # ascending
    assert [m.rowid for m in db.thread_messages(CHAT, limit=1)] == [msgs[2]]
    assert [m.rowid for m in db.thread_messages(CHAT, limit=5, before_rowid=msgs[2])] == msgs[:2]

    assert db.recent_messages(GROUP) == msg_mod.recent_messages(conn, schema, GROUP)
    recent = db.recent_messages(GROUP)
    assert [m.rowid for m in recent] == msgs[3:][::-1]  # newest first
    assert not any(m.enriched for m in recent)  # default enrich=False
    assert [m.rowid for m in db.recent_messages(GROUP, limit=1)] == [msgs[5]]

    assert db.message(msgs[2]) == msg_mod.message(conn, schema, msgs[2])
    m3 = db.message(msgs[2])
    assert m3 is not None and m3.enriched and [a.guid for a in m3.attachments] == ["ATT-1"]
    assert db.message(10_000) is None
    assert db.message(msgs[2], enrich=False) is not None

    by_guid = db.messages_by_guid(["SYN-MSG-000001", "SYN-MSG-000004"])
    assert by_guid == msg_mod.messages_by_guid(conn, schema, ["SYN-MSG-000001", "SYN-MSG-000004"])
    assert set(by_guid) == {"SYN-MSG-000001", "SYN-MSG-000004"}
    assert db.messages_by_guid([]) == {}

    targets = db.reply_targets(["SYN-MSG-000001"])
    assert targets == msg_mod.reply_targets(conn, ["SYN-MSG-000001"])
    assert targets["SYN-MSG-000001"].text == "hello there"
    blob_only = [m.text for m in db.messages_after(0, enrich=False) if m.rowid == msgs[1]]
    assert blob_only == ["blob only"]


def test_attachment_methods_match_module_functions(
    populated: dict[str, Any], reader: tuple[sqlite3.Connection, Schema]
) -> None:
    db: ChatDB = populated["fx"].db
    conn, _schema = reader
    m3, m5 = populated["msgs"][2], populated["msgs"][4]

    assert db.attachments_for([m3]) == att_mod.attachments_for(conn, [m3])
    assert [a.guid for a in db.attachments_for([m3])[m3]] == ["ATT-1"]  # plugin payload filtered
    assert sorted(a.guid for a in db.attachments_for([m3], include_plugin_payloads=True)[m3]) == [
        "ATT-1",
        "ATT-2",
    ]

    a1 = db.attachment("ATT-1")
    assert a1 == att_mod.attachment_by_guid(conn, "ATT-1")
    assert a1 is not None and a1.mime_type == "image/png"
    assert db.attachment("nope") is None

    assert db.chat_attachments(CHAT) == att_mod.chat_attachments(conn, CHAT)
    assert [a.guid for a in db.chat_attachments(CHAT)] == ["ATT-1"]
    assert len(db.chat_attachments(CHAT, include_plugin_payloads=True)) == 2

    payload = db.payload(m5)
    assert payload == att_mod.payload_for(conn, m5)
    assert isinstance(payload, bytes)
    assert db.payload(populated["msgs"][0]) is None

    img = db.link_image(m5)
    assert img == embedded_image(payload)
    assert img is not None and img.startswith(b"\x89PNG")
    assert db.link_image(populated["msgs"][0]) is None
    assert db.link_image(10_000) is None


def test_chat_methods_match_module_functions(
    populated: dict[str, Any], reader: tuple[sqlite3.Connection, Schema]
) -> None:
    db: ChatDB = populated["fx"].db
    conn, _schema = reader
    one, group, msgs = populated["one"], populated["group"], populated["msgs"]

    assert db.chats() == chats_mod.chats_by_activity(conn)
    assert [c.rowid for c in db.chats()] == [group, one]
    assert [c.rowid for c in db.chats(limit=1)] == [group]

    assert db.chat(GROUP) == chats_mod.chat(conn, GROUP)
    g = db.chat(GROUP)
    assert g is not None and g.is_group and g.display_name == "Synthetic"
    assert db.chat("any;-;nobody") is None
    assert db.chat_by_rowid(one) == chats_mod.chat_by_rowid(conn, one)
    assert db.chat_by_rowid(10_000) is None

    assert db.participants(group) == chats_mod.participants(conn, group)
    assert sorted(db.participants(group)) == [PHONE, EMAIL]
    assert db.participants_map() == chats_mod.participants_map(conn)
    assert set(db.participants_map()) == {one, group}

    assert db.chat_services([one, group]) == chats_mod.chat_services(conn, [one, group])
    assert db.chat_services([one, group]) == {one: "iMessage", group: "iMessage"}

    assert db.last_rowid_for(one) == chats_mod.last_rowid_for(conn, one) == msgs[2]
    assert db.chats_changed_since(0) == chats_mod.chats_changed_since(conn, 0)
    assert {a.chat_rowid: a.last_rowid for a in db.chats_changed_since(0)} == {
        c.rowid: c.last_rowid for c in db.chats()
    }
    assert db.chats_changed_since(msgs[-1]) == []
    assert db.unread_count(one, 0) == chats_mod.unread_count(conn, one, 0)
    assert db.unread_count(one, 0) == 2  # incoming only by default
    assert db.unread_count(one, 0, incoming_only=False) == 3
    assert db.unread_count(one, msgs[2]) == 0

    lite = db.lite_messages(msgs)
    assert lite == chats_mod.lite_messages(conn, msgs)
    assert set(lite) == set(msgs)
    assert [a.guid for a in lite[msgs[2]].attachments] == ["ATT-1"]

    assert db.one_to_one_activity() == chats_mod.one_to_one_activity(conn)
    assert db.one_to_one_activity() == [(PHONE, msgs[2])]

    assert db.find_chat([PHONE]) == chats_mod.find_chat(conn, [PHONE])
    match = db.find_chat([PHONE])
    assert match is not None and match.chat_guid == CHAT and not match.is_group
    assert db.find_chat([PHONE], exclude=[PHONE]) is None
    gm = db.find_chat([PHONE, EMAIL])
    assert gm is not None and gm.chat_guid == GROUP and gm.is_group
    # custom key: everything collapses to one person -> still a 1:1 lookup for one address
    def digits(a: str) -> str | None:
        return "".join(filter(str.isdigit, a))[-10:]

    assert db.find_chat(["(555) 000-1234"], key=digits) == match


def test_search_matches_module_function(
    populated: dict[str, Any], reader: tuple[sqlite3.Connection, Schema]
) -> None:
    db: ChatDB = populated["fx"].db
    conn, _schema = reader
    assert db.search("hello") == search_mod.search(conn, "hello")
    hits = db.search("hello")
    assert [h.rowid for h in hits] == [populated["msgs"][0]]
    assert db.search("blob") and db.search("blob")[0].rowid == populated["msgs"][1]
    assert db.search("") == []
    assert len(db.search("e", limit=2)) == 2


def test_search_chat_guid_passes_through(
    populated: dict[str, Any], reader: tuple[sqlite3.Connection, Schema]
) -> None:
    db: ChatDB = populated["fx"].db
    conn, _schema = reader
    hits = db.search("hello")
    assert hits, "the populated fixture must contain a hello hit"
    guid = hits[0].chat_guid
    assert db.search("hello", chat_guid=guid) == search_mod.search(conn, "hello", chat_guid=guid)
    assert all(h.chat_guid == guid for h in db.search("hello", chat_guid=guid))
    assert db.search("hello", chat_guid="any;-;no-such-chat") == []


# ---------------------------------------------------------------------------
# open()
# ---------------------------------------------------------------------------


def test_open_convenience_returns_probed_chatdb(
    populated: dict[str, Any], traced: list[TracedConnection]
) -> None:
    fx: FixtureDB = populated["fx"]
    db = open(fx.path)
    assert isinstance(db, ChatDB)
    assert db.path == fx.path and db.timeout == 5.0 and db.readonly_uri is True
    # probed once: the schema is already cached and the probe connection closed
    assert len(traced) == 1 and traced[0].closed
    assert db.schema.profile == fx.profile
    assert len(traced) == 1
    assert db.max_rowid() == populated["msgs"][-1]
    assert open(fx.path, timeout=0.5).timeout == 0.5
    # the package-level name is the same function; builtins.open is untouched
    assert imessage_chatdb.open is open
    assert open.__module__ == "imessage_chatdb.db"
    assert isinstance(imessage_chatdb.open(fx.path), ChatDB)


def test_open_surfaces_access_error_immediately(tmp_path: Path) -> None:
    with pytest.raises(ChatDBAccessError) as info:
        open(tmp_path / "does-not-exist" / "chat.db")
    assert sys.executable in str(info.value)
    assert "Full Disk Access" in str(info.value)
    # ChatDB() itself stays lazy
    lazy = ChatDB(tmp_path / "does-not-exist" / "chat.db")
    with pytest.raises(ChatDBAccessError):
        lazy.max_rowid()


def test_open_on_every_profile(fixture_db: FixtureDB) -> None:
    db = open(fixture_db.path)
    assert db.schema.profile == fixture_db.profile
    assert db.initial_cursor() == Cursor()
    assert db.chats() == [] and db.messages_after(0) == []


# ---------------------------------------------------------------------------
# package surface
# ---------------------------------------------------------------------------


def test_package_reexports_resolve_to_submodule_objects() -> None:
    from imessage_chatdb import dates, errors, handles, link_preview, models, services, typedstream

    assert imessage_chatdb.ChatDB is ChatDB
    assert imessage_chatdb.open is open
    assert imessage_chatdb.Message is models.Message
    assert imessage_chatdb.Attachment is models.Attachment
    assert imessage_chatdb.ChatSummary is models.ChatSummary
    assert imessage_chatdb.apple_to_unix is dates.apple_to_unix
    assert imessage_chatdb.APPLE_EPOCH_OFFSET == dates.APPLE_EPOCH_OFFSET == 978307200
    assert imessage_chatdb.extract_text is typedstream.extract_text
    assert imessage_chatdb.effective_text is typedstream.effective_text
    assert imessage_chatdb.LINK_BALLOON == link_preview.LINK_BALLOON
    assert imessage_chatdb.parse_link_preview is link_preview.parse_link_preview
    assert imessage_chatdb.Service is services.Service
    assert imessage_chatdb.service_family is services.service_family
    assert imessage_chatdb.address_key is handles.address_key
    assert imessage_chatdb.ChatDBBusy is errors.ChatDBBusy
    assert imessage_chatdb.CursorAhead is errors.CursorAhead
    assert imessage_chatdb.Schema is Schema
    assert imessage_chatdb.__version__ == "0.2.0"


def test_all_names_are_real_attributes_not_lazy() -> None:
    # every __all__ entry is bound in the module namespace (no __getattr__ hook)
    missing = [n for n in imessage_chatdb.__all__ if n not in vars(imessage_chatdb)]
    assert missing == []
    assert not hasattr(imessage_chatdb, "__getattr__")
    assert len(imessage_chatdb.__all__) == len(set(imessage_chatdb.__all__))
    with pytest.raises(AttributeError):
        _ = imessage_chatdb.definitely_not_a_public_name  # type: ignore[attr-defined]


def test_design_4_3_names_are_exported() -> None:
    expected = {
        "APPLE_EPOCH_OFFSET", "apple_to_unix", "unix_to_apple", "apple_to_datetime",
        "extract_text", "effective_text", "clean_text",
        "KeyedArchive",
        "LINK_BALLOON", "SKIP_IMG", "LinkPreview", "parse_link_preview", "embedded_image",
        "sniff_image_mime",
        "Reaction", "classify_reaction", "parse_associated_guid",
        "Service", "normalize_service", "service_family",
        "address_key", "is_email", "is_group_style",
        "extract_urls", "snippet",
        "Message", "Attachment", "ReplyTarget", "LiteMessage", "Chat", "ChatSummary",
        "ChatMatch", "ChatActivity", "SearchHit",
        "ChatDBError", "ChatDBAccessError", "ChatDBBusy", "CursorAhead", "SchemaError",
        "Cursor", "Event", "poll_once", "watch", "run_watch",
        "ChatDB", "open_connection", "DEFAULT_CHATDB",
        "Schema", "REQUIRED", "OPTIONAL", "build_message_select",
        "__version__",
    }
    assert expected <= set(imessage_chatdb.__all__)
    # deliberately an attribute but not an export: ``open`` (would shadow the builtin
    # under ``import *``); ``watch`` is the generator function and IS exported
    assert "open" not in imessage_chatdb.__all__
    assert imessage_chatdb.open is open


def test_star_import_does_not_shadow_builtin_open_and_exports_watch() -> None:
    import builtins

    namespace: dict[str, Any] = {}
    exec("from imessage_chatdb import *", namespace)  # noqa: S102 - our own package
    assert "open" not in namespace, "import * must not replace builtins.open"
    assert namespace["ChatDB"] is ChatDB and namespace["Cursor"] is Cursor
    assert builtins.open is not open
    # the polling module is ``imessage_chatdb.polling``; ``watch`` is its generator
    import imessage_chatdb.polling as polling_module

    assert imessage_chatdb.polling is polling_module
    assert namespace["watch"] is polling_module.watch is imessage_chatdb.watch
    assert polling_module.Cursor is Cursor and callable(polling_module.poll_once)


def test_open_rejects_a_sqlite_file_that_is_not_chat_db(
    tmp_path: Path, safe_db_path: Callable[[Path], Path]
) -> None:
    """A non-Messages database raises the library's SchemaError, not a raw sqlite3 error."""
    from imessage_chatdb.errors import ChatDBError, SchemaError

    path = safe_db_path(tmp_path / "not-chat.db")
    w = sqlite3.connect(path, isolation_level=None)
    try:
        w.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY, note TEXT)")
    finally:
        w.close()

    with pytest.raises(SchemaError) as info:
        open(path)
    assert isinstance(info.value, ChatDBError)
    assert info.value.column.startswith("message.")
    assert info.value.column in str(info.value)

    # the lazy constructor raises the same error from the first call that needs the
    # schema, and nothing stale is cached.  (Schema-free methods such as max_rowid()
    # keep SQLite's own error: the schema is introspected lazily, DESIGN.md 4.1.)
    lazy = ChatDB(path)
    with pytest.raises(SchemaError):
        _ = lazy.schema
    with pytest.raises(SchemaError):
        lazy.messages_after(0)
    with pytest.raises(SchemaError):
        lazy.max_date_edited()
    with pytest.raises(SchemaError):
        lazy.refresh_schema()
    with pytest.raises(sqlite3.OperationalError):
        lazy.max_rowid()


def test_open_rejects_a_chat_db_missing_a_required_column(
    make_db: MakeDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``refresh_schema`` checks required columns and leaves the cache alone on failure."""
    from imessage_chatdb.errors import SchemaError

    fx = make_db("macos27")
    db = ChatDB(fx.path)
    good = db.schema  # cached, valid

    original = Schema.has

    def no_guid(self: Schema, table: str, column: str) -> bool:
        if (table, column) == ("message", "guid"):
            return False
        return original(self, table, column)

    monkeypatch.setattr(Schema, "has", no_guid)
    with pytest.raises(SchemaError) as info:
        db.refresh_schema()
    assert info.value.column == "message.guid"
    assert db.schema is good  # the failed refresh did not replace the cache
    with pytest.raises(SchemaError) as info2:
        open(fx.path)
    assert info2.value.column == "message.guid"


# ---------------------------------------------------------------------------
# suite-wide read-only guard: every facade method over the pristine database
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("readonly_uri", [True, False])
def test_every_facade_method_leaves_the_pristine_file_untouched(
    pristine_db: Pristine, readonly_uri: bool
) -> None:
    """Runs each public query method (both connect modes) on the session-wide
    sha256-guarded database; the digest is re-checked here and at teardown."""
    db = ChatDB(pristine_db.path, readonly_uri=readonly_uri)
    top = db.max_rowid()
    assert top == pristine_db.rowids[-1]
    assert db.max_date_edited() == 0
    assert db.initial_cursor() == Cursor(top, 0)
    msgs = db.messages_after(0)
    assert [m.rowid for m in msgs] == list(pristine_db.rowids)
    assert all(m.chat_rowid is not None and m.chat_guid is not None for m in msgs)
    assert msgs[-1].text == "synthetic blob" and len(msgs[-1].attachments) == 1
    assert db.messages_edited_after(0) == ([], 0)
    assert [m.rowid for m in db.thread_messages(pristine_db.chat_guid)] == list(
        pristine_db.rowids[:3]
    )
    assert [m.rowid for m in db.recent_messages(pristine_db.chat_guid, limit=2)] == list(
        reversed(pristine_db.rowids[1:3])
    )
    first = db.message(pristine_db.rowids[0])
    assert first is not None and first.text == "synthetic 0"
    assert set(db.messages_by_guid([first.guid])) == {first.guid}
    assert db.reply_targets([first.guid])[first.guid].text == "synthetic 0"
    att = db.attachments_for(pristine_db.rowids)[pristine_db.rowids[-1]]
    assert [a.guid for a in att] == [pristine_db.attachment_guid]
    assert db.attachment(pristine_db.attachment_guid) is not None
    assert [a.guid for a in db.chat_attachments(pristine_db.group_guid)] == [
        pristine_db.attachment_guid
    ]
    assert db.payload(pristine_db.rowids[0]) is None
    assert db.link_image(pristine_db.rowids[0]) is None
    assert [c.guid for c in db.chats()] == [pristine_db.group_guid, pristine_db.chat_guid]
    assert db.chat(pristine_db.chat_guid) is not None
    assert db.chat_by_rowid(pristine_db.group_rowid) is not None
    assert len(db.participants(pristine_db.group_rowid)) == 2
    assert set(db.participants_map()) == {pristine_db.chat_rowid, pristine_db.group_rowid}
    assert db.chat_services([pristine_db.chat_rowid])[pristine_db.chat_rowid] == "iMessage"
    assert db.last_rowid_for(pristine_db.chat_rowid) == pristine_db.rowids[2]
    assert [a.chat_rowid for a in db.chats_changed_since(0)] == [
        pristine_db.group_rowid, pristine_db.chat_rowid
    ]
    assert db.chats_changed_since(pristine_db.rowids[2]) == [
        ChatActivity(pristine_db.group_rowid, pristine_db.rowids[-1])
    ]
    assert db.unread_count(pristine_db.chat_rowid, 0) == 3
    assert set(db.lite_messages(pristine_db.rowids)) == set(pristine_db.rowids)
    assert db.one_to_one_activity() == [(PHONE, pristine_db.rowids[2])]
    found = db.find_chat([PHONE])
    assert found is not None and found.chat_guid == pristine_db.chat_guid
    assert [h.rowid for h in db.search("synthetic")] == list(reversed(pristine_db.rowids))
    events, cur = db.poll(Cursor())
    assert len(events) == pristine_db.message_count and cur.rowid == top
    with db.connection() as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    assert sha256_of(pristine_db.path) == pristine_db.sha256
