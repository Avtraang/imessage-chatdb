"""Tests for edit detection via ``date_edited`` (DESIGN.md 8.4 edits, T6).

``messages_edited_after`` and ``max_date_edited`` on synthetic databases.
The mark is a raw Apple integer: it is never converted and never regresses.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from imessage_chatdb.connection import open_connection
from imessage_chatdb.messages import (
    max_date_edited,
    max_rowid,
    messages_after,
    messages_edited_after,
)
from imessage_chatdb.schema import Schema
from tests.conftest import FixtureDB, MakeDB
from tests.fixtures import builders as b

PHONE = "+15550001234"
CHAT = "any;-;+15550001234"

# Raw Apple ns values well above the 1e11 seconds/ns heuristic boundary.
EDIT_A = b.BASE_DATE_NS + 300 * b.DATE_STEP_NS
EDIT_B = b.BASE_DATE_NS + 500 * b.DATE_STEP_NS
EDIT_C = b.BASE_DATE_NS + 700 * b.DATE_STEP_NS


@pytest.fixture
def reader(fixture_db: FixtureDB) -> Iterator[tuple[sqlite3.Connection, Schema]]:
    conn = open_connection(fixture_db.path)
    try:
        yield conn, Schema(conn)
    finally:
        conn.close()


def test_only_rows_past_the_mark_ascending_by_date_edited(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    unedited = b.add_message(w, chat, text="never", date_edited=0)
    late = b.add_message(w, chat, text="edited last", date_edited=EDIT_C)
    early = b.add_message(w, chat, text="edited first", date_edited=EDIT_A)
    mid = b.add_message(w, chat, text="edited middle", date_edited=EDIT_B)

    msgs, mark = messages_edited_after(conn, schema, 0)
    assert [m.rowid for m in msgs] == [early, mid, late]  # by date_edited, not ROWID
    assert mark == EDIT_C and isinstance(mark, int)
    assert unedited not in {m.rowid for m in msgs}
    assert [m.date_edited for m in msgs] == [EDIT_A, EDIT_B, EDIT_C]  # raw ints on the model
    assert all(m.enriched for m in msgs)

    msgs, mark = messages_edited_after(conn, schema, EDIT_A)  # strict >
    assert [m.rowid for m in msgs] == [mid, late] and mark == EDIT_C

    msgs, mark = messages_edited_after(conn, schema, EDIT_C)
    assert msgs == [] and mark == EDIT_C


def test_mark_never_regresses(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    b.add_message(w, chat, text="x", date_edited=EDIT_A)
    ahead = EDIT_C + 1
    msgs, mark = messages_edited_after(conn, schema, ahead)
    assert msgs == [] and mark == ahead
    # a mark equal to the newest edit is kept as is (strict >)
    assert messages_edited_after(conn, schema, EDIT_A) == ([], EDIT_A)
    # a fresh Schema on an empty-result window keeps the caller's mark too
    assert messages_edited_after(conn, Schema(conn), EDIT_B) == ([], EDIT_B)


def test_in_place_edit_missed_by_rowid_cursor_is_caught(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    b.add_message(w, chat, text="one", handle=PHONE)
    target = b.add_message(w, chat, text="two", handle=PHONE)
    b.add_message(w, chat, text="three", handle=PHONE)
    cursor = max_rowid(conn)
    edit_mark = max_date_edited(conn, schema)
    assert edit_mark == 0

    # Messages.app edits rewrite the row in place: text changes, ROWID does not.
    w.execute("UPDATE message SET text = ? WHERE ROWID = ?", ("two (edited)", target))
    b.set_date_edited(w, target, EDIT_B)

    assert messages_after(conn, schema, cursor) == []  # the ROWID cursor is blind
    msgs, new_mark = messages_edited_after(conn, schema, edit_mark)
    assert [m.rowid for m in msgs] == [target]
    assert msgs[0].text == "two (edited)"
    assert msgs[0].date_edited == EDIT_B
    assert new_mark == EDIT_B
    assert max_date_edited(conn, schema) == EDIT_B
    # the next round is quiet until another edit lands
    assert messages_edited_after(conn, schema, new_mark) == ([], new_mark)
    b.set_date_edited(w, target, EDIT_C)
    msgs, newer = messages_edited_after(conn, schema, new_mark)
    assert [m.rowid for m in msgs] == [target] and newer == EDIT_C


def test_max_date_edited(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    assert max_date_edited(conn, schema) == 0  # empty table
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    b.add_message(w, chat, text="a", date_edited=0)
    assert max_date_edited(conn, schema) == 0
    b.add_message(w, chat, text="b", date_edited=EDIT_B)
    b.add_message(w, chat, text="c", date_edited=EDIT_A)
    got = max_date_edited(conn, schema)
    assert got == EDIT_B and isinstance(got, int)
    _, mark = messages_edited_after(conn, schema, 0)
    assert mark == got


def test_edited_rows_are_enriched_by_default(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    rid = b.add_message(w, chat, text=None, has_att=1, date_edited=EDIT_A)
    b.add_attachment(w, rid, "ATT-E", "image/jpeg", "e.jpg", "~/synthetic/e.jpg")
    msgs, _ = messages_edited_after(conn, schema, 0)
    assert len(msgs) == 1 and [a.guid for a in msgs[0].attachments] == ["ATT-E"]
    lean, _ = messages_edited_after(conn, schema, 0, enrich=False)
    assert lean[0].enriched is False and lean[0].attachments == ()


def test_without_date_edited_column_returns_empty_and_mark(make_db: MakeDB) -> None:
    """A schema lacking ``message.date_edited`` -> ``([], mark)`` and ``max_date_edited == 0``."""
    fx = make_db("macos14")
    w = fx.writer
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    b.add_message(w, chat, text="x")
    w.execute("ALTER TABLE message DROP COLUMN date_edited")
    assert not b.has_column(w, "message", "date_edited")
    conn = open_connection(fx.path)
    try:
        schema = Schema(conn)
        assert "message.date_edited" in schema.optional_missing
        assert messages_edited_after(conn, schema, 0) == ([], 0)
        assert messages_edited_after(conn, schema, 4242) == ([], 4242)
        assert max_date_edited(conn, schema) == 0
        # other fetches keep working and render the field as None
        got = messages_after(conn, schema, 0)
        assert len(got) == 1 and got[0].date_edited is None
    finally:
        conn.close()
