"""Tests for ``imessage_chatdb.messages`` (DESIGN.md 8.4 messages, T6).

Synthetic databases only (``make_db`` under ``tmp_path``); synthetic handles
only.  Query counts are verified with ``sqlite3.Connection.set_trace_callback``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import FrozenInstanceError

import pytest

from imessage_chatdb import messages as msgs_mod
from imessage_chatdb.connection import open_connection
from imessage_chatdb.link_preview import LINK_BALLOON
from imessage_chatdb.messages import (
    enrich,
    max_rowid,
    message,
    messages_after,
    messages_by_guid,
    recent_messages,
    reply_targets,
    row_to_message,
    thread_messages,
)
from imessage_chatdb.models import Attachment, Message, ReplyTarget
from imessage_chatdb.schema import Schema
from tests.conftest import FixtureDB, MakeDB
from tests.fixtures import builders as b
from tests.fixtures.keyed_archive_writer import make_link_payload
from tests.fixtures.typedstream_writer import encode_attributed_body

PHONE = "+15550001234"
OTHER = "+15550009876"
EMAIL = "test@example.invalid"
CHAT = "any;-;+15550001234"
GROUP = "any;+;chat9001"


@pytest.fixture
def reader(fixture_db: FixtureDB) -> Iterator[tuple[sqlite3.Connection, Schema]]:
    """A read-only connection + schema for the current profile's database."""
    conn = open_connection(fixture_db.path)
    try:
        yield conn, Schema(conn)
    finally:
        conn.close()


def _trace(conn: sqlite3.Connection) -> list[str]:
    stmts: list[str] = []
    conn.set_trace_callback(stmts.append)
    return stmts


def _att_queries(stmts: list[str]) -> list[str]:
    return [s for s in stmts if "message_attachment_join" in s]


def _reply_queries(stmts: list[str]) -> list[str]:
    return [s for s in stmts if "WHERE m.guid IN" in s]


# ---------------------------------------------------------------------------
# messages_after
# ---------------------------------------------------------------------------


def test_messages_after_is_strict_ascending_and_limited(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    ids = [b.add_message(w, chat, text=f"m{i}", handle=PHONE) for i in range(5)]

    got = messages_after(conn, schema, ids[1])
    assert [m.rowid for m in got] == ids[2:]
    assert all(isinstance(m, Message) for m in got)

    assert [m.rowid for m in messages_after(conn, schema, 0)] == ids
    assert messages_after(conn, schema, ids[-1]) == []
    assert [m.rowid for m in messages_after(conn, schema, 0, limit=2)] == ids[:2]
    assert [m.rowid for m in messages_after(conn, schema, ids[0], limit=1)] == [ids[1]]


def test_messages_after_on_empty_database(reader: tuple[sqlite3.Connection, Schema]) -> None:
    conn, schema = reader
    assert messages_after(conn, schema, 0) == []
    assert max_rowid(conn) == 0


def test_max_rowid(fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]) -> None:
    w = fixture_db.writer
    conn, _ = reader
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    last = 0
    for i in range(3):
        last = b.add_message(w, chat, text=str(i))
    assert max_rowid(conn) == last


# ---------------------------------------------------------------------------
# row_to_message
# ---------------------------------------------------------------------------


def test_booleans_are_real_bools(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    mine = b.add_message(w, chat, text="out", is_from_me=1, has_att=1)
    b.add_attachment(w, mine, "ATT-1", "image/png", "a.png", "~/synthetic/a.png")
    theirs = b.add_message(w, chat, text="in", is_from_me=0, handle=PHONE, has_att=0)

    by_id = {m.rowid: m for m in messages_after(conn, schema, 0)}
    assert by_id[mine].is_from_me is True
    assert by_id[mine].has_attachments is True
    assert by_id[theirs].is_from_me is False
    assert by_id[theirs].has_attachments is False
    assert by_id[mine].sender_handle is None  # handle_id 0 -> no handle row
    assert by_id[theirs].sender_handle == PHONE


def test_text_rule_matches_effective_text(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    blob = encode_attributed_body("from the blob")
    both = b.add_message(w, chat, text="column wins", body=blob)
    blob_only = b.add_message(w, chat, text=None, body=blob)
    empty_plus_blob = b.add_message(w, chat, text="", body=blob)
    none_only = b.add_message(w, chat, text=None, body=None)
    empty_only = b.add_message(w, chat, text="", body=None)
    empty_bad_blob = b.add_message(w, chat, text="", body=b"\x04\x0bstreamtyped garbage")

    by_id = {m.rowid: m for m in messages_after(conn, schema, 0, enrich=False)}
    assert by_id[both].text == "column wins"
    assert by_id[both].text_column == "column wins"
    assert by_id[blob_only].text == "from the blob"
    assert by_id[blob_only].text_column is None
    assert by_id[empty_plus_blob].text == "from the blob"
    assert by_id[empty_plus_blob].text_column == ""
    assert by_id[none_only].text is None
    assert by_id[empty_only].text == ""
    assert by_id[empty_bad_blob].text is None  # the relay's result for "" + undecodable blob


def test_link_parsed_only_for_url_balloon(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    payload = make_link_payload("https://example.invalid/page", "A title", "Sum", "Site")
    linked = b.add_message(w, chat, text="https://example.invalid/page", balloon=LINK_BALLOON,
                           payload=payload)
    other_balloon = b.add_message(w, chat, text="x", balloon="com.apple.other", payload=payload)
    no_balloon = b.add_message(w, chat, text="y", balloon=None, payload=payload)
    balloon_no_payload = b.add_message(w, chat, text="z", balloon=LINK_BALLOON, payload=None)

    by_id = {m.rowid: m for m in messages_after(conn, schema, 0)}
    link = by_id[linked].link
    assert link is not None
    assert link.url == "https://example.invalid/page"
    assert link.title == "A title"
    assert link.summary == "Sum"
    assert link.site == "Site"
    assert by_id[linked].balloon_bundle_id == LINK_BALLOON
    assert by_id[other_balloon].link is None
    assert by_id[other_balloon].balloon_bundle_id == "com.apple.other"
    assert by_id[no_balloon].link is None
    assert by_id[balloon_no_payload].link is None


# optional message column -> Message field
_OPTIONAL_FIELDS = {
    "date_read": "date_read",
    "date_delivered": "date_delivered",
    "date_edited": "date_edited",
    "date_retracted": "date_retracted",
    "service": "service_raw",
    "associated_message_emoji": "associated_emoji",
    "balloon_bundle_id": "balloon_bundle_id",
    "thread_originator_guid": "reply_to_guid",
    "thread_originator_part": "reply_to_part",
    "item_type": "item_type",
    "group_action_type": "group_action_type",
    "group_title": "group_title",
    "filter_action": "filter_action",
    "is_spam": "is_spam",
    "expressive_send_style_id": "expressive_send_style_id",
}


def test_missing_optional_columns_are_none_and_present_ones_are_read(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    chat = b.add_chat(w, GROUP, style=43, display_name="Synthetic group", handles=[PHONE, OTHER])
    b.add_message(w, chat, text="target", handle=PHONE, guid="TARGET-1")
    rid = b.add_message(
        w, chat, text="full", handle=OTHER, service="SMS", assoc_emoji="🎉",
        reply_to="TARGET-1", reply_to_part="0:0:5", balloon="com.apple.x",
        date_read=11, date_delivered=12, date_edited=13, date_retracted=14,
        item_type=1, group_action_type=2, group_title="gt", filter_action=3, is_spam=1,
        expressive_send_style_id="com.apple.MobileSMS.expressivesend.impact",
    )
    m = message(conn, schema, rid)
    assert m is not None
    assert m.rowid == rid and m.reply_to is not None and m.reply_to.guid == "TARGET-1"
    expected = {
        "date_read": 11, "date_delivered": 12, "date_edited": 13, "date_retracted": 14,
        "service_raw": "SMS", "associated_emoji": "🎉", "balloon_bundle_id": "com.apple.x",
        "reply_to_guid": "TARGET-1", "reply_to_part": "0:0:5", "item_type": 1,
        "group_action_type": 2, "group_title": "gt", "filter_action": 3, "is_spam": True,
        "expressive_send_style_id": "com.apple.MobileSMS.expressivesend.impact",
    }
    for column, field in _OPTIONAL_FIELDS.items():
        value = getattr(m, field)
        if f"message.{column}" in schema.optional_missing:
            assert value is None, (fixture_db.profile, field)
        else:
            assert value == expected[field], (fixture_db.profile, field)
    # chat fields and group detection
    assert m.chat_guid == GROUP and m.chat_style == 43 and m.is_group is True
    assert m.chat_display_name == "Synthetic group"
    assert m.chat_identifier == "chat9001"
    assert m.service_raw is None or m.service == "SMS"


def test_is_spam_is_bool_or_none(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    rid = b.add_message(w, chat, text="x", is_spam=0)
    m = message(conn, schema, rid)
    assert m is not None
    if schema.has("message", "is_spam"):
        assert m.is_spam is False
    else:
        assert m.is_spam is None


def test_row_to_message_tolerates_rows_without_optional_aliases(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    """A narrower row (only the required aliases) still builds a Message."""
    w = fixture_db.writer
    conn, _ = reader
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    rid = b.add_message(w, chat, text="narrow", handle=PHONE)
    row = conn.execute(
        """SELECT m.ROWID AS rowid, m.guid AS guid, m.text AS text,
                  m.attributedBody AS attributed_body, m.date AS date, m.is_from_me AS is_from_me,
                  m.cache_has_attachments AS has_attachments,
                  c.ROWID AS chat_rowid, c.guid AS chat_guid
           FROM message m JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
           JOIN chat c ON c.ROWID = cmj.chat_id WHERE m.ROWID = ?""",
        (rid,),
    ).fetchone()
    m = row_to_message(row)
    assert m.rowid == rid and m.text == "narrow" and m.chat_guid == CHAT
    assert m.sender_handle is None and m.link is None and m.service_raw is None
    assert m.date_edited is None and m.reply_to_guid is None and m.chat_style is None
    assert m.enriched is False and m.attachments == () and m.reply_to is None


def test_messages_are_frozen(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    rid = b.add_message(w, chat, text="x")
    m = message(conn, schema, rid)
    assert m is not None
    with pytest.raises(FrozenInstanceError):
        m.text = "y"  # type: ignore[misc]


def test_tapback_and_service_pass_through(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    b.add_message(w, chat, text="hello", guid="ORIG-1", handle=PHONE)
    tap = b.add_message(w, chat, text=None, assoc_guid="p:0/ORIG-1", assoc_type=2000,
                        is_from_me=1, service="iMessage")
    m = message(conn, schema, tap)
    assert m is not None
    assert m.associated_guid == "p:0/ORIG-1" and m.associated_type == 2000
    assert m.is_tapback is True
    r = m.reaction
    assert r is not None and r.action == "add" and r.kind == "love" and r.target_guid == "ORIG-1"
    plain = message(conn, schema, tap - 1)
    assert plain is not None and plain.reaction is None and plain.is_tapback is False
    assert m.service_raw == "iMessage" and m.service == "iMessage"


# ---------------------------------------------------------------------------
# orphans
# ---------------------------------------------------------------------------


def test_orphans_excluded_by_default_and_included_on_request(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    joined = b.add_message(w, chat, text="joined")
    orphan = b.add_message(w, chat, text="orphan", join=False)

    assert [m.rowid for m in messages_after(conn, schema, 0)] == [joined]
    both = messages_after(conn, schema, 0, include_orphans=True)
    assert [m.rowid for m in both] == [joined, orphan]
    o = both[1]
    assert o.text == "orphan"
    # LEFT JOIN rows carry NULL chat columns; ``chat_rowid`` / ``chat_guid`` are
    # typed ``int | None`` / ``str | None`` for exactly this case.
    assert o.chat_rowid is None
    assert o.chat_guid is None
    assert o.chat_style is None
    assert o.chat_identifier is None
    assert o.chat_display_name is None
    assert o.is_group is False
    assert o.chat_name is None
    assert o.to_dict()["chat_guid"] is None and o.to_dict()["is_group"] is False
    assert message(conn, schema, orphan) is None


# ---------------------------------------------------------------------------
# thread_messages / recent_messages
# ---------------------------------------------------------------------------


def test_thread_messages_desc_limit_then_reversed_with_paging(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    other = b.add_chat(w, "any;-;+15550009876", handles=[OTHER])
    ids = []
    for i in range(10):
        ids.append(b.add_message(w, chat, text=f"t{i}"))
        b.add_message(w, other, text=f"o{i}")  # interleaved noise in another chat

    page1 = thread_messages(conn, schema, CHAT, limit=3)
    assert [m.rowid for m in page1] == ids[7:]  # ascending, newest three
    page2 = thread_messages(conn, schema, CHAT, limit=3, before_rowid=page1[0].rowid)
    assert [m.rowid for m in page2] == ids[4:7]
    page3 = thread_messages(conn, schema, CHAT, limit=3, before_rowid=page2[0].rowid)
    assert [m.rowid for m in page3] == ids[1:4]
    page4 = thread_messages(conn, schema, CHAT, limit=3, before_rowid=page3[0].rowid)
    assert [m.rowid for m in page4] == ids[:1]
    assert thread_messages(conn, schema, CHAT, limit=3, before_rowid=page4[0].rowid) == []
    # a falsy before_rowid means "no bound", as in the relay
    unbounded = thread_messages(conn, schema, CHAT, limit=3, before_rowid=0)
    assert [m.rowid for m in unbounded] == ids[7:]
    assert [m.rowid for m in thread_messages(conn, schema, CHAT)] == ids  # default limit 50
    assert thread_messages(conn, schema, "any;-;nobody@example.invalid") == []
    assert all(m.enriched for m in page1)


def test_recent_messages_newest_first_not_enriched(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    ids = [b.add_message(w, chat, text=f"r{i}", has_att=1) for i in range(6)]
    for i in ids:
        b.add_attachment(w, i, f"ATT-{i}", "image/png", "p.png", "~/synthetic/p.png")

    stmts = _trace(conn)
    got = recent_messages(conn, schema, CHAT, limit=4)
    assert [m.rowid for m in got] == list(reversed(ids))[:4]
    assert all(m.enriched is False and m.attachments == () for m in got)
    assert _att_queries(stmts) == []
    assert [m.rowid for m in recent_messages(conn, schema, CHAT)] == list(reversed(ids))
    rich = recent_messages(conn, schema, CHAT, limit=2, enrich=True)
    assert all(m.enriched and len(m.attachments) == 1 for m in rich)


# ---------------------------------------------------------------------------
# message / messages_by_guid
# ---------------------------------------------------------------------------


def test_message_by_rowid_and_by_guid(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    a = b.add_message(w, chat, text="a", guid="G-A")
    bb = b.add_message(w, chat, text="b", guid="G-B")

    m = message(conn, schema, a)
    assert m is not None and m.guid == "G-A" and m.enriched is True
    assert message(conn, schema, 999_999) is None
    lite = message(conn, schema, a, enrich=False)
    assert lite is not None and lite.enriched is False

    got = messages_by_guid(conn, schema, ["G-B", "G-A", "G-A", "", "G-MISSING"])
    assert set(got) == {"G-A", "G-B"}
    assert got["G-A"].rowid == a and got["G-B"].rowid == bb
    assert all(m.enriched for m in got.values())

    stmts = _trace(conn)
    assert messages_by_guid(conn, schema, []) == {}
    assert stmts == []


# ---------------------------------------------------------------------------
# enrich / reply_targets
# ---------------------------------------------------------------------------


def _build_enrich_fixture(w: sqlite3.Connection) -> dict[str, int]:
    chat = b.add_chat(w, GROUP, style=43, handles=[PHONE, OTHER])
    with_att = b.add_message(w, chat, text=None, handle=PHONE, has_att=1, guid="HAS-ATT")
    b.add_attachment(w, with_att, "ATT-REAL", "image/png", "photo.png", "~/synthetic/photo.png")
    b.add_attachment(w, with_att, "ATT-PAYLOAD", None, "x.pluginPayloadAttachment", None)
    # has_attachments = 0 but a join row exists: must NOT be looked up
    stale = b.add_message(w, chat, text="stale flag", has_att=0, guid="STALE")
    b.add_attachment(w, stale, "ATT-STALE", "image/png", "stale.png", "~/synthetic/stale.png")
    reply = b.add_message(w, chat, text="replying", handle=OTHER, reply_to="HAS-ATT", guid="REPLY")
    reply2 = b.add_message(w, chat, text="also", handle=OTHER, reply_to="STALE", guid="REPLY2")
    reply3 = b.add_message(w, chat, text="dup", handle=OTHER, reply_to="STALE", guid="REPLY3")
    dangling = b.add_message(w, chat, text="?", reply_to="NO-SUCH-GUID", guid="DANGLING")
    plain = b.add_message(w, chat, text="plain", guid="PLAIN")
    return {
        "chat": chat, "with_att": with_att, "stale": stale, "reply": reply,
        "reply2": reply2, "reply3": reply3, "dangling": dangling, "plain": plain,
    }


def test_enrich_issues_one_attachment_and_one_reply_query_for_has_attachments_rows_only(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    ids = _build_enrich_fixture(w)
    raw = messages_after(conn, schema, 0, enrich=False)
    assert all(m.enriched is False for m in raw)

    stmts = _trace(conn)
    rich = enrich(conn, raw)
    att_q = _att_queries(stmts)
    reply_q = _reply_queries(stmts)
    assert len(att_q) == 1, stmts
    assert len(reply_q) == 1, stmts
    assert len(stmts) == 2, stmts
    # only the has_attachments row is in the IN list (trace shows expanded SQL)
    assert f"({ids['with_att']})" in att_q[0]
    assert str(ids["stale"]) not in att_q[0].split("IN")[-1]
    # reply query carries each distinct target guid once
    tail = reply_q[0].split("IN")[-1]
    assert tail.count("'HAS-ATT'") == 1 and tail.count("'STALE'") == 1
    assert "'NO-SUCH-GUID'" in tail

    by_id = {m.rowid: m for m in rich}
    assert all(m.enriched is True for m in rich)
    assert [m.rowid for m in rich] == [m.rowid for m in raw]
    assert raw[0].enriched is False  # inputs untouched

    atts = by_id[ids["with_att"]].attachments
    assert isinstance(atts, tuple) and [a.guid for a in atts] == ["ATT-REAL"]
    assert all(isinstance(a, Attachment) for a in atts)
    assert by_id[ids["stale"]].attachments == ()
    assert by_id[ids["plain"]].attachments == () and by_id[ids["plain"]].reply_to is None

    rt = by_id[ids["reply"]].reply_to
    assert isinstance(rt, ReplyTarget)
    assert rt.guid == "HAS-ATT" and rt.text == "" and rt.is_from_me is False
    assert rt.sender_handle == PHONE
    assert by_id[ids["reply2"]].reply_to == by_id[ids["reply3"]].reply_to
    rt2 = by_id[ids["reply2"]].reply_to
    assert rt2 is not None and rt2.text == "stale flag"
    assert by_id[ids["dangling"]].reply_to is None
    assert by_id[ids["dangling"]].reply_to_guid == "NO-SUCH-GUID"


def test_enrich_skips_queries_when_nothing_to_fetch(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    b.add_message(w, chat, text="one")
    b.add_message(w, chat, text="two")
    raw = messages_after(conn, schema, 0, enrich=False)

    stmts = _trace(conn)
    rich = enrich(conn, raw)
    assert stmts == []
    assert all(m.enriched and m.attachments == () and m.reply_to is None for m in rich)
    assert enrich(conn, []) == []
    assert stmts == []


def test_default_fetches_enrich_with_one_query_each(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    _build_enrich_fixture(w)
    stmts = _trace(conn)
    messages_after(conn, schema, 0)
    assert len(_att_queries(stmts)) == 1 and len(_reply_queries(stmts)) == 1
    del stmts[:]
    thread_messages(conn, schema, GROUP, limit=50)
    assert len(_att_queries(stmts)) == 1 and len(_reply_queries(stmts)) == 1


def test_reply_targets_cleaned_untruncated_and_empty_for_attachment_only(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, _ = reader
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    long_text = "word " * 60  # 300 chars, > 120
    b.add_message(w, chat, text=f"￼  {long_text}\n\n tail", guid="LONG", handle=PHONE)
    b.add_message(w, chat, text=None, body=None, has_att=1, guid="ATT-ONLY", is_from_me=1)
    b.add_message(w, chat, text=None, body=encode_attributed_body("  blob\ttext "), guid="BLOB")
    b.add_message(w, chat, text="", guid="EMPTY")

    got = reply_targets(conn, ["LONG", "ATT-ONLY", "BLOB", "EMPTY", "MISSING", "LONG", ""])
    assert set(got) == {"LONG", "ATT-ONLY", "BLOB", "EMPTY"}
    assert got["LONG"].text == " ".join(long_text.split()) + " tail"
    assert len(got["LONG"].text) > 120 and "￼" not in got["LONG"].text
    assert got["LONG"].is_from_me is False and got["LONG"].sender_handle == PHONE
    assert got["ATT-ONLY"].text == ""
    assert got["ATT-ONLY"].is_from_me is True and got["ATT-ONLY"].sender_handle is None
    assert got["BLOB"].text == "blob text"
    assert got["EMPTY"].text == ""
    # the relay's "You"/"Attachment"/[:120] strings are the relay adapter's, not to_dict's
    assert got["ATT-ONLY"].to_dict() == {
        "guid": "ATT-ONLY", "text": "", "is_from_me": True, "sender_handle": None,
    }
    assert got["LONG"].to_dict()["text"] == got["LONG"].text  # untruncated

    stmts = _trace(conn)
    assert reply_targets(conn, []) == {}
    assert reply_targets(conn, ["", None]) == {}  # type: ignore[list-item]
    assert stmts == []


# ---------------------------------------------------------------------------
# all public query functions run on every profile (smoke)
# ---------------------------------------------------------------------------


def test_every_query_function_runs_on_profile(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    ids = _build_enrich_fixture(w)
    assert msgs_mod.max_rowid(conn) == ids["plain"]
    assert msgs_mod.max_date_edited(conn, schema) == 0
    assert len(msgs_mod.messages_after(conn, schema, 0)) == 7
    edited, mark = msgs_mod.messages_edited_after(conn, schema, 0)
    assert edited == [] and mark == 0
    assert len(msgs_mod.thread_messages(conn, schema, GROUP)) == 7
    assert len(msgs_mod.recent_messages(conn, schema, GROUP)) == 7
    assert msgs_mod.message(conn, schema, ids["plain"]) is not None
    assert set(msgs_mod.messages_by_guid(conn, schema, ["PLAIN"])) == {"PLAIN"}
    assert set(msgs_mod.reply_targets(conn, ["PLAIN"])) == {"PLAIN"}


def test_profiles_fixture_covers_all_four(make_db: MakeDB) -> None:
    for profile in ("macos14", "macos15", "macos26", "macos27"):
        fx = make_db(profile)
        conn = open_connection(fx.path)
        try:
            assert Schema(conn).profile == profile
        finally:
            conn.close()
