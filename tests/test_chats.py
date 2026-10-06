"""chats.py: activity listing, lookups, participants, services, unread, previews (DESIGN.md 8.4)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from imessage_chatdb import chats
from imessage_chatdb.connection import open_connection
from imessage_chatdb.models import Chat, ChatActivity, ChatSummary, LiteMessage
from tests.conftest import FixtureDB, MakeDB
from tests.fixtures.builders import (
    BASE_DATE_NS,
    add_attachment,
    add_chat,
    add_handle,
    add_message,
    has_column,
)
from tests.fixtures.typedstream_writer import encode_attributed_body

A = "+15550001234"
B = "+15550005678"
C = "c@example.invalid"


@pytest.fixture
def reader(fixture_db: FixtureDB) -> Iterator[sqlite3.Connection]:
    conn = open_connection(fixture_db.path)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# chats_by_activity
# ---------------------------------------------------------------------------


def test_chats_by_activity_excludes_empty_orders_desc_and_limits(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    one = add_chat(w, f"any;-;{A}", 45, handles=[A])
    group = add_chat(w, "any;+;chat1", 43, display_name="Crew", handles=[A, B])
    add_chat(w, f"any;-;{B}", 45, handles=[B])  # no messages: excluded
    m1 = add_message(w, one, text="first", handle=A)
    m2 = add_message(w, group, text="second", handle=B)
    m3 = add_message(w, one, text="third", is_from_me=1)

    out = chats.chats_by_activity(reader)
    assert [c.rowid for c in out] == [one, group]
    assert all(isinstance(c, ChatSummary) for c in out)
    assert out[0].last_rowid == m3 and out[1].last_rowid == m2
    assert m1 < m2 < m3
    assert out[0].last_date == BASE_DATE_NS + 2 * 1_000_000_000
    assert out[1].display_name == "Crew" and out[1].is_group is True
    assert out[0].chat_identifier == A and out[0].is_group is False

    assert [c.rowid for c in chats.chats_by_activity(reader, limit=1)] == [one]
    assert chats.chats_by_activity(reader, limit=0) == []


def test_chats_by_activity_fills_optional_chat_columns(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    rid = add_chat(w, "any;+;g", 43, service_name="SMS", group_id="gid-1", is_archived=1)
    add_message(w, rid, text="x", handle=A)
    (c,) = chats.chats_by_activity(reader)
    assert c.service_name == "SMS"
    assert c.group_id == "gid-1"
    assert c.is_archived is True
    if has_column(w, "chat", "is_filtered"):
        assert c.is_filtered is False
    else:
        assert c.is_filtered is None
    d = c.to_dict()
    assert d["last_rowid"] == c.last_rowid and isinstance(d["last_date"], float)


# ---------------------------------------------------------------------------
# chat / chat_by_rowid
# ---------------------------------------------------------------------------


def test_chat_lookups(fixture_db: FixtureDB, reader: sqlite3.Connection) -> None:
    w = fixture_db.writer
    rid = add_chat(w, "any;+;g", 43, display_name="Crew", service_name="iMessage", handles=[A])
    c = chats.chat(reader, "any;+;g")
    assert isinstance(c, Chat)
    assert c == chats.chat_by_rowid(reader, rid)
    assert (c.rowid, c.guid, c.style, c.chat_identifier, c.display_name) == (
        rid,
        "any;+;g",
        43,
        "g",
        "Crew",
    )
    assert c.service_name == "iMessage"
    assert c.is_archived is False
    assert c.is_group is True and c.chat_name == "Crew"
    assert c.is_filtered is (False if has_column(w, "chat", "is_filtered") else None)
    assert chats.chat(reader, "any;-;nope") is None
    assert chats.chat_by_rowid(reader, rid + 99) is None


# ---------------------------------------------------------------------------
# participants
# ---------------------------------------------------------------------------


def test_participants_and_map(fixture_db: FixtureDB, reader: sqlite3.Connection) -> None:
    w = fixture_db.writer
    g1 = add_chat(w, "any;+;g1", 43, handles=[A, B, C])
    g2 = add_chat(w, "any;+;g2", 43, handles=[B])
    empty = add_chat(w, "any;+;g3", 43)
    assert sorted(chats.participants(reader, g1)) == sorted([A, B, C])
    assert chats.participants(reader, g2) == [B]
    assert chats.participants(reader, empty) == []
    pm = chats.participants_map(reader)
    assert set(pm) == {g1, g2}
    assert sorted(pm[g1]) == sorted([A, B, C]) and pm[g2] == [B]


# ---------------------------------------------------------------------------
# chat_services
# ---------------------------------------------------------------------------


def test_chat_services_precedence(fixture_db: FixtureDB, reader: sqlite3.Connection) -> None:
    w = fixture_db.writer
    # newest outgoing (SMS) beats a newer incoming (RCS)
    c1 = add_chat(w, "any;-;c1", 45, service_name="iMessage")
    add_message(w, c1, text="a", is_from_me=1, service="SMS")
    add_message(w, c1, text="b", handle=A, service="RCS")
    # no outgoing: newest message with a service wins; Lite folds into iMessage
    c2 = add_chat(w, "any;-;c2", 45, service_name="SMS")
    add_message(w, c2, text="a", handle=A, service="RCS")
    add_message(w, c2, text="b", handle=A, service="iMessageLite")
    # empty-string services are ignored at both levels
    c3 = add_chat(w, "any;-;c3", 45, service_name="SMS")
    add_message(w, c3, text="a", is_from_me=1, service="RCS")
    add_message(w, c3, text="b", is_from_me=1, service="")
    add_message(w, c3, text="c", handle=A, service=None)
    # no message services at all: chat.service_name
    c4 = add_chat(w, "any;-;c4", 45, service_name="sms")
    add_message(w, c4, text="a", handle=A, service="")
    # nothing anywhere
    c5 = add_chat(w, "any;-;c5", 45)
    # unknown service strings -> None
    c6 = add_chat(w, "any;-;c6", 45, service_name="Bogus")
    add_message(w, c6, text="a", is_from_me=1, service="Carrier")

    out = chats.chat_services(reader, [c1, c2, c3, c4, c5, c6])
    assert out == {
        c1: "SMS",
        c2: "iMessage",
        c3: "RCS",
        c4: "SMS",
        c5: None,
        c6: None,
    }
    assert chats.chat_services(reader, []) == {}
    assert chats.chat_services(reader, [c1 + 1000]) == {}


def test_chat_services_raises_on_sql_failure(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    rid = add_chat(fx.writer, "any;-;c1", 45)
    fx.writer.execute("DROP TABLE chat_message_join")
    conn = open_connection(fx.path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            chats.chat_services(conn, [rid])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# last_rowid_for / unread_count
# ---------------------------------------------------------------------------


def test_last_rowid_for(fixture_db: FixtureDB, reader: sqlite3.Connection) -> None:
    w = fixture_db.writer
    c1 = add_chat(w, "any;-;c1", 45)
    c2 = add_chat(w, "any;-;c2", 45)
    assert chats.last_rowid_for(reader, c1) == 0
    add_message(w, c1, text="a")
    m = add_message(w, c1, text="b")
    add_message(w, c2, text="c")
    assert chats.last_rowid_for(reader, c1) == m
    assert chats.last_rowid_for(reader, c2 + 50) == 0


def test_unread_count(fixture_db: FixtureDB, reader: sqlite3.Connection) -> None:
    w = fixture_db.writer
    c1 = add_chat(w, "any;-;c1", 45)
    other = add_chat(w, "any;-;c2", 45)
    m0 = add_message(w, c1, text="old", handle=A)
    add_message(w, c1, text="in1", handle=A)
    add_message(w, c1, text="out", is_from_me=1)
    add_message(w, c1, text="in2", handle=A)
    add_message(w, other, text="elsewhere", handle=B)
    assert chats.unread_count(reader, c1, m0) == 2
    assert chats.unread_count(reader, c1, m0, incoming_only=False) == 3
    assert chats.unread_count(reader, c1, 0) == 3
    assert chats.unread_count(reader, c1, 10**6) == 0


# ---------------------------------------------------------------------------
# chats_changed_since
# ---------------------------------------------------------------------------


def test_chats_changed_since_empty_past_the_newest_rowid(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    assert chats.chats_changed_since(reader, 0) == []  # no messages at all
    c1 = add_chat(w, "any;-;c1", 45)
    m = add_message(w, c1, text="a", handle=A)
    assert chats.chats_changed_since(reader, m) == []
    assert chats.chats_changed_since(reader, m + 1000) == []


def test_chats_changed_since_several_chats_newest_first(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    c1 = add_chat(w, "any;-;c1", 45)
    c2 = add_chat(w, "any;-;c2", 45)
    group = add_chat(w, "any;+;g", 43, handles=[A, B])
    m1 = add_message(w, c1, text="1", handle=A)
    add_message(w, c2, text="2", handle=B)
    add_message(w, group, text="3", handle=A)
    m4 = add_message(w, c2, text="4", is_from_me=1)
    m5 = add_message(w, group, text="5", is_from_me=1)
    m6 = add_message(w, c1, text="6", handle=A)

    out = chats.chats_changed_since(reader, m1)
    assert all(isinstance(a, ChatActivity) for a in out)
    assert out == [
        ChatActivity(c1, m6),
        ChatActivity(group, m5),
        ChatActivity(c2, m4),
    ]
    assert out[0].to_dict() == {"chat_rowid": c1, "last_rowid": m6}
    # the newest ROWIDs agree with chats_by_activity's last_rowid
    by_activity = {c.rowid: c.last_rowid for c in chats.chats_by_activity(reader)}
    assert {a.chat_rowid: a.last_rowid for a in out} == by_activity


def test_chats_changed_since_excludes_chats_with_only_old_messages(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    stale = add_chat(w, "any;-;stale", 45)
    live = add_chat(w, "any;-;live", 45)
    add_chat(w, "any;-;empty", 45)  # no messages: never listed
    add_message(w, stale, text="old 1", handle=A)
    cursor = add_message(w, stale, text="old 2", handle=A)
    m3 = add_message(w, live, text="new", handle=B)
    unjoined = add_message(w, live, text="orphan", handle=B, join=False)
    assert unjoined > m3

    out = chats.chats_changed_since(reader, cursor)
    assert out == [ChatActivity(live, m3)]  # stale is out; orphan rows do not count


def test_chats_changed_since_rowid_zero_lists_every_chat_with_messages(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    c1 = add_chat(w, "any;-;c1", 45)
    c2 = add_chat(w, "any;-;c2", 45)
    add_chat(w, "any;-;c3", 45)  # no messages
    ma = add_message(w, c1, text="a", handle=A)
    mb = add_message(w, c2, text="b", handle=B)
    out = chats.chats_changed_since(reader, 0)
    assert out == [ChatActivity(c2, mb), ChatActivity(c1, ma)]
    assert [a.chat_rowid for a in out] == [c.rowid for c in chats.chats_by_activity(reader)]


# ---------------------------------------------------------------------------
# lite_messages
# ---------------------------------------------------------------------------


def test_lite_messages_facts(fixture_db: FixtureDB, reader: sqlite3.Connection) -> None:
    w = fixture_db.writer
    c1 = add_chat(w, "any;-;c1", 45)
    plain = add_message(w, c1, text="  hello ￼  world ", handle=A)
    blob = add_message(w, c1, body=encode_attributed_body("from blob"), is_from_me=1)
    tap = add_message(w, c1, text="Loved x", handle=B, assoc_type=2000, assoc_guid="p:0/G")
    emo = add_message(w, c1, text="x", handle=B, assoc_type=2006, assoc_emoji="🔥")
    nothing = add_message(w, c1, handle=A)

    out = chats.lite_messages(reader, [plain, blob, tap, emo, nothing, 99_999])
    assert set(out) == {plain, blob, tap, emo, nothing}
    assert all(isinstance(v, LiteMessage) for v in out.values())

    assert out[plain].text == "hello world"
    assert out[plain].sender_handle == A
    assert out[plain].is_from_me is False
    assert out[plain].associated_type == 0
    assert out[plain].has_attachments is False
    assert out[plain].attachments == ()

    assert out[blob].text == "from blob"
    assert out[blob].is_from_me is True
    assert out[blob].sender_handle is None

    assert out[tap].associated_type == 2000 and out[tap].text == "Loved x"
    assert out[emo].associated_type == 2006
    expected_emoji = "🔥" if has_column(w, "message", "associated_message_emoji") else None
    assert out[emo].associated_emoji == expected_emoji
    assert out[tap].associated_emoji is None

    assert out[nothing].text == ""
    assert chats.lite_messages(reader, []) == {}


def test_lite_messages_attachments_only_for_has_attachments_rows(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    c1 = add_chat(w, "any;-;c1", 45)
    with_att = add_message(w, c1, handle=A, has_att=1)
    add_attachment(w, with_att, "ATT-1", "image/jpeg", "IMG_1.jpeg", "~/x/IMG_1.jpeg")
    add_attachment(w, with_att, "ATT-2", None, "card.pluginPayloadAttachment", "~/x/card")
    flag_off = add_message(w, c1, handle=A, has_att=0)
    add_attachment(w, flag_off, "ATT-3", "image/png", "IMG_3.png", "~/x/IMG_3.png")
    flag_on_empty = add_message(w, c1, handle=A, has_att=1)

    out = chats.lite_messages(reader, [with_att, flag_off, flag_on_empty])
    assert out[with_att].has_attachments is True
    assert [a.guid for a in out[with_att].attachments] == ["ATT-1"]
    a = out[with_att].attachments[0]
    assert a.message_rowid == with_att and a.mime_type == "image/jpeg"
    assert a.transfer_name == "IMG_1.jpeg" and a.is_plugin_payload is False
    assert out[flag_off].has_attachments is False and out[flag_off].attachments == ()
    assert out[flag_on_empty].has_attachments is True and out[flag_on_empty].attachments == ()


# ---------------------------------------------------------------------------
# one_to_one_activity
# ---------------------------------------------------------------------------


def test_one_to_one_activity(fixture_db: FixtureDB, reader: sqlite3.Connection) -> None:
    w = fixture_db.writer
    one_a = add_chat(w, f"any;-;{A}", 45)
    one_b = add_chat(w, f"any;-;{B}", 45)
    add_chat(w, f"any;-;{C}", 45)  # no messages
    group = add_chat(w, "any;+;g", 43, handles=[A, B])
    add_message(w, one_a, text="1", handle=A)
    add_message(w, group, text="2", handle=A)
    mb = add_message(w, one_b, text="3", handle=B)
    ma = add_message(w, one_a, text="4", is_from_me=1)
    out = chats.one_to_one_activity(reader)
    assert sorted(out) == sorted([(A, ma), (B, mb)])
    assert all(isinstance(ci, str) and isinstance(last, int) for ci, last in out)


def test_functions_accept_a_connection_without_row_factory(make_db: MakeDB) -> None:
    """The library sets ``sqlite3.Row`` on its own cursors (the writer has no row factory)."""
    fx = make_db("macos27")
    w = fx.writer
    rid = add_chat(w, "any;-;c1", 45, handles=[A])
    m = add_message(w, rid, text="hi", handle=A)
    add_handle(w, B)
    assert w.row_factory is None
    assert chats.chats_by_activity(w)[0].last_rowid == m
    assert chats.chats_changed_since(w, 0) == [ChatActivity(rid, m)]
    assert chats.participants(w, rid) == [A]
    assert chats.lite_messages(w, [m])[m].text == "hi"
    assert chats.chat_services(w, [rid]) == {rid: "iMessage"}
