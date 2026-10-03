"""chats.find_chat: one-to-one and group matching (DESIGN.md 4.4, 8.4)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from imessage_chatdb.chats import find_chat
from imessage_chatdb.connection import open_connection
from imessage_chatdb.handles import address_key
from imessage_chatdb.models import ChatMatch
from tests.conftest import FixtureDB, MakeDB
from tests.fixtures.builders import add_chat, add_message

A = "+15550001234"
A_EMAIL = "a@example.invalid"
B = "+15550005678"
C = "+15550009012"
ME = "+15550000000"

#: A contact-aware ``key`` like the relay's ``person_key``: phone and e-mail of
#: the same person compare equal; everyone else falls back to ``address_key``.
_PEOPLE = {address_key(A): "person-a", address_key(A_EMAIL): "person-a"}


def person_key(addr: str) -> str | None:
    k = address_key(addr)
    return _PEOPLE.get(k, k)


@pytest.fixture
def reader(fixture_db: FixtureDB) -> Iterator[sqlite3.Connection]:
    conn = open_connection(fixture_db.path)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# one-to-one
# ---------------------------------------------------------------------------


def test_one_to_one_default_key_normalises_formatting(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    rid = add_chat(w, f"any;-;{A}", 45, handles=[A])
    m = add_message(w, rid, text="hi", handle=A)
    match = find_chat(reader, ["1 (555) 000-1234"])
    assert isinstance(match, ChatMatch)
    assert match == ChatMatch(
        chat_rowid=rid,
        chat_guid=f"any;-;{A}",
        chat_identifier=A,
        display_name=None,
        is_group=False,
        last_rowid=m,
    )
    assert find_chat(reader, [B]) is None


def test_one_to_one_custom_key_collapses_phone_and_email(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    rid = add_chat(w, f"any;-;{A}", 45, handles=[A])
    assert find_chat(reader, [A_EMAIL]) is None  # default key: no e-mail chat
    match = find_chat(reader, [A_EMAIL], key=person_key)
    assert match is not None and match.chat_rowid == rid
    assert match.last_rowid == 0  # no messages yet


def test_one_to_one_newest_wins_then_imessage_guid_tie_break(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    sms = add_chat(w, f"SMS;-;{A}", 45, identifier=A)
    im = add_chat(w, f"iMessage;-;{A}", 45, identifier=A)
    any_ = add_chat(w, f"any;-;{A}", 45, identifier=A)
    # all three empty: the tie goes to the guid starting with "iMessage"
    match = find_chat(reader, [A])
    assert match is not None and match.chat_rowid == im
    # activity beats the tie-break
    add_message(w, sms, text="x", handle=A)
    match = find_chat(reader, [A])
    assert match is not None and match.chat_rowid == sms
    m = add_message(w, any_, text="y", handle=A)
    match = find_chat(reader, [A])
    assert match is not None and (match.chat_rowid, match.last_rowid) == (any_, m)


def test_one_to_one_ignores_groups_and_null_identifiers(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    add_chat(w, "any;+;g", 43, handles=[A])
    add_chat(w, "any;-;weird", 45, identifier=None)
    w.execute("UPDATE chat SET chat_identifier = NULL WHERE guid = 'any;-;weird'")
    assert find_chat(reader, [A]) is None


# ---------------------------------------------------------------------------
# exclude / want
# ---------------------------------------------------------------------------


def test_empty_want_returns_none_without_query(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    add_chat(fixture_db.writer, f"any;-;{A}", 45, handles=[A])
    statements: list[str] = []
    reader.set_trace_callback(statements.append)
    assert find_chat(reader, []) is None
    assert find_chat(reader, [ME], exclude=[ME]) is None
    assert find_chat(reader, [ME, "(555) 000-0000"], exclude=[ME]) is None
    reader.set_trace_callback(None)
    assert statements == []


def test_exclude_removes_self_from_group_membership(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    rid = add_chat(w, "any;+;g", 43, display_name="Us", handles=[ME, A, B])
    # Without exclude, self is a participant and [A, B] is not the full set.
    assert find_chat(reader, [A, B]) is None
    assert find_chat(reader, [A, B, ME]) is not None
    # With exclude, self is dropped on both sides.
    for addrs in ([A, B], [A, B, ME]):
        match = find_chat(reader, addrs, exclude=[ME])
        assert match is not None
        assert (match.chat_rowid, match.is_group, match.display_name) == (rid, True, "Us")


# ---------------------------------------------------------------------------
# group branch
# ---------------------------------------------------------------------------


def test_two_addresses_collapsing_to_one_person_take_the_group_branch(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    one = add_chat(w, f"any;-;{A}", 45, handles=[A])
    add_message(w, one, text="hi", handle=A)
    # want == {"person-a"}, but len(addresses) == 2 -> group branch -> no group -> None
    assert find_chat(reader, [A, A_EMAIL], key=person_key) is None
    # a group whose only (non-self) person is person-a does match on that branch
    solo = add_chat(w, "any;+;solo", 43, handles=[A_EMAIL])
    match = find_chat(reader, [A, A_EMAIL], key=person_key)
    assert match is not None and match.chat_rowid == solo and match.is_group is True
    # and a single address still takes the 1:1 branch
    match = find_chat(reader, [A_EMAIL], key=person_key)
    assert match is not None and match.chat_rowid == one


def test_group_membership_is_joins_union_incoming_senders(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    rid = add_chat(w, "any;+;g", 43, handles=[A])  # join row for A only
    add_message(w, rid, text="from B", handle=B)  # B only ever appears as a sender
    add_message(w, rid, text="mine", is_from_me=1, handle=C)  # outgoing: C is NOT a member
    assert find_chat(reader, [A]) is None  # 1:1 branch, no such 1:1 chat
    assert find_chat(reader, [A, B, C]) is None
    match = find_chat(reader, [A, B])
    assert match is not None and match.chat_rowid == rid


def test_group_requires_exact_set_equality(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    add_chat(w, "any;+;abc", 43, handles=[A, B, C])
    assert find_chat(reader, [A, B]) is None
    assert find_chat(reader, [A, B, C, ME]) is None
    match = find_chat(reader, [C, "555-000-5678", A])  # order and formatting irrelevant
    assert match is not None and match.chat_guid == "any;+;abc"


def test_group_newest_activity_wins(fixture_db: FixtureDB, reader: sqlite3.Connection) -> None:
    w = fixture_db.writer
    old = add_chat(w, "any;+;old", 43, handles=[A, B])
    new = add_chat(w, "any;+;new", 43, handles=[A, B])
    add_message(w, new, text="1", handle=A)
    m_old = add_message(w, old, text="2", handle=B)
    match = find_chat(reader, [A, B])
    assert match is not None and (match.chat_rowid, match.last_rowid) == (old, m_old)
    m_new = add_message(w, new, text="3", is_from_me=1)
    match = find_chat(reader, [A, B])
    assert match is not None and (match.chat_rowid, match.last_rowid) == (new, m_new)


def test_group_match_fields(fixture_db: FixtureDB, reader: sqlite3.Connection) -> None:
    w = fixture_db.writer
    rid = add_chat(w, "any;+;chat9", 43, display_name="Nine", handles=[A, B])
    match = find_chat(reader, [A, B])
    assert match == ChatMatch(
        chat_rowid=rid,
        chat_guid="any;+;chat9",
        chat_identifier="chat9",
        display_name="Nine",
        is_group=True,
        last_rowid=0,
    )


def test_group_with_no_participants_matches_nothing(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    add_chat(fx.writer, "any;+;empty", 43)
    conn = open_connection(fx.path)
    try:
        assert find_chat(conn, [A, B]) is None
    finally:
        conn.close()
