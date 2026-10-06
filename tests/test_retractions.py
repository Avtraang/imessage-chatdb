"""Tests for unsend ("retracted") events (0.2): ``messages_retracted_after``,
``max_date_retracted``, ``Cursor.retract_mark`` and the ``"retracted"`` event kind.

Synthetic databases only.  Two of the four schema profiles (``macos14``,
``macos15``) lack ``message.date_retracted``, so every profile-parametrised
test here also covers "a schema without the column" for free; the builder's
``set_date_retracted`` is a no-op there.
"""

from __future__ import annotations

import inspect
import sqlite3
import threading
from collections.abc import Iterator

import pytest

from imessage_chatdb.connection import open_connection
from imessage_chatdb.db import ChatDB
from imessage_chatdb.messages import (
    max_date_retracted,
    messages_after,
    messages_retracted_after,
)
from imessage_chatdb.polling import Cursor, Event, poll_once, run_watch, watch
from imessage_chatdb.schema import Schema
from tests.conftest import FixtureDB, MakeDB
from tests.fixtures import builders as b

PHONE = "+15550001234"
CHAT = "any;-;+15550001234"

EDIT_A = b.BASE_DATE_NS + 300 * b.DATE_STEP_NS
EDIT_B = b.BASE_DATE_NS + 500 * b.DATE_STEP_NS
RETRACT_A = b.BASE_DATE_NS + 400 * b.DATE_STEP_NS
RETRACT_B = b.BASE_DATE_NS + 600 * b.DATE_STEP_NS
RETRACT_C = b.BASE_DATE_NS + 800 * b.DATE_STEP_NS


def _kinds(events: list[Event]) -> list[str]:
    return [e.kind for e in events]


def _rowids(events: list[Event]) -> list[int]:
    return [e.message.rowid for e in events]


def _has_retract(fx: FixtureDB) -> bool:
    return bool(fx.db.schema.has("message", "date_retracted"))


@pytest.fixture
def seeded(fixture_db: FixtureDB) -> tuple[FixtureDB, int, int]:
    """A chat with one existing message; returns ``(fixture, chat_rowid, first_msg_rowid)``."""
    chat = b.add_chat(fixture_db.writer, CHAT, handles=[PHONE])
    first = b.add_message(fixture_db.writer, chat, text="existing", handle=PHONE)
    return fixture_db, chat, first


@pytest.fixture
def reader(fixture_db: FixtureDB) -> Iterator[tuple[sqlite3.Connection, Schema]]:
    conn = open_connection(fixture_db.path)
    try:
        yield conn, Schema(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# messages.py: messages_retracted_after / max_date_retracted
# ---------------------------------------------------------------------------


def test_only_rows_past_the_mark_ascending_by_date_retracted(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    kept = b.add_message(w, chat, text="kept", date_retracted=0)
    late = b.add_message(w, chat, text=None, date_retracted=RETRACT_C)
    early = b.add_message(w, chat, text=None, date_retracted=RETRACT_A)
    mid = b.add_message(w, chat, text=None, date_retracted=RETRACT_B)

    msgs, mark = messages_retracted_after(conn, schema, 0)
    if not schema.has("message", "date_retracted"):
        assert (msgs, mark) == ([], 0)
        assert messages_retracted_after(conn, schema, 4242) == ([], 4242)
        assert max_date_retracted(conn, schema) == 0
        assert "message.date_retracted" in schema.optional_missing
        # other fetches keep working and render the field as None
        assert all(m.date_retracted is None for m in messages_after(conn, schema, 0))
        return
    assert [m.rowid for m in msgs] == [early, mid, late]  # by date_retracted, not ROWID
    assert mark == RETRACT_C and isinstance(mark, int)
    assert kept not in {m.rowid for m in msgs}
    assert [m.date_retracted for m in msgs] == [RETRACT_A, RETRACT_B, RETRACT_C]  # raw ints
    assert all(m.enriched for m in msgs)
    assert all(m.text is None for m in msgs)  # the row as it is now

    msgs, mark = messages_retracted_after(conn, schema, RETRACT_A)  # strict >
    assert [m.rowid for m in msgs] == [mid, late] and mark == RETRACT_C
    assert messages_retracted_after(conn, schema, RETRACT_C) == ([], RETRACT_C)
    lean, _ = messages_retracted_after(conn, schema, 0, enrich=False)
    assert lean and not any(m.enriched for m in lean)


def test_mark_never_regresses_and_max_date_retracted(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    w = fixture_db.writer
    conn, schema = reader
    assert max_date_retracted(conn, schema) == 0  # empty table
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    b.add_message(w, chat, text="a", date_retracted=0)
    assert max_date_retracted(conn, schema) == 0
    b.add_message(w, chat, text="b", date_retracted=RETRACT_B)
    b.add_message(w, chat, text="c", date_retracted=RETRACT_A)
    got = max_date_retracted(conn, schema)
    expected = RETRACT_B if schema.has("message", "date_retracted") else 0
    assert got == expected and isinstance(got, int)
    _, mark = messages_retracted_after(conn, schema, 0)
    assert mark == got
    ahead = RETRACT_C + 1
    assert messages_retracted_after(conn, schema, ahead) == ([], ahead)
    assert messages_retracted_after(conn, schema, RETRACT_B) == ([], RETRACT_B)


def test_unsend_in_place_is_invisible_to_the_rowid_cursor(
    fixture_db: FixtureDB, reader: tuple[sqlite3.Connection, Schema]
) -> None:
    """Messages.app keeps the row, sets ``date_retracted`` and clears the text."""
    w = fixture_db.writer
    conn, schema = reader
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    target = b.add_message(w, chat, text="oops", is_from_me=1)
    top = fixture_db.db.max_rowid()
    b.set_date_retracted(w, target, RETRACT_A, clear_text=True)
    assert messages_after(conn, schema, top) == []  # the ROWID cursor is blind
    msgs, mark = messages_retracted_after(conn, schema, 0)
    if not schema.has("message", "date_retracted"):
        assert (msgs, mark) == ([], 0)
        return
    assert [m.rowid for m in msgs] == [target] and mark == RETRACT_A
    assert msgs[0].text is None and msgs[0].text_column is None
    assert msgs[0].date_retracted == RETRACT_A
    assert msgs[0].date_retracted_unix is not None


def test_dropping_the_column_from_a_newer_profile(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    w = fx.writer
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    b.add_message(w, chat, text="x", date_retracted=RETRACT_A)
    w.execute("ALTER TABLE message DROP COLUMN date_retracted")
    assert not b.has_column(w, "message", "date_retracted")
    conn = open_connection(fx.path)
    try:
        schema = Schema(conn)
        assert messages_retracted_after(conn, schema, 0) == ([], 0)
        assert max_date_retracted(conn, schema) == 0
    finally:
        conn.close()
    assert fx.db.initial_cursor().retract_mark == 0
    assert poll_once(fx.db, Cursor(rowid=fx.db.max_rowid())) == (
        [],
        Cursor(rowid=fx.db.max_rowid()),
    )


# ---------------------------------------------------------------------------
# Cursor JSON
# ---------------------------------------------------------------------------


def test_cursor_json_round_trip_with_and_without_retract_mark() -> None:
    c = Cursor(rowid=42, edit_mark=EDIT_A, retract_mark=RETRACT_A)
    d = c.to_json()
    assert d == {"rowid": 42, "edit_mark": EDIT_A, "retract_mark": RETRACT_A}
    assert Cursor.from_json(d) == c
    assert isinstance(Cursor.from_json(d).retract_mark, int)
    # a state file written by 0.1 has no retract_mark: it reads as 0
    assert Cursor.from_json({"rowid": 42, "edit_mark": EDIT_A}) == Cursor(42, EDIT_A, 0)
    assert Cursor.from_json({"rowid": 42, "edit_mark": EDIT_A, "retract_mark": None}) == Cursor(
        42, EDIT_A
    )
    assert Cursor.from_json({"retract_mark": str(RETRACT_B)}) == Cursor(retract_mark=RETRACT_B)
    assert Cursor.from_json({}) == Cursor()
    assert Cursor(1, 2) == Cursor(rowid=1, edit_mark=2, retract_mark=0)  # positional 0.1 form


# ---------------------------------------------------------------------------
# poll_once
# ---------------------------------------------------------------------------


def test_initial_cursor_seeds_retract_mark(seeded: tuple[FixtureDB, int, int]) -> None:
    fx, chat, first = seeded
    assert fx.db.initial_cursor() == Cursor(rowid=first)
    b.set_date_retracted(fx.writer, first, RETRACT_A)
    cur = fx.db.initial_cursor()
    assert cur.retract_mark == fx.db.max_date_retracted()
    assert cur.retract_mark == (RETRACT_A if _has_retract(fx) else 0)
    assert cur == Cursor(rowid=first, edit_mark=0, retract_mark=cur.retract_mark)
    # "now" means no replay of the unsend
    assert poll_once(fx.db, cur, include_retractions=True) == ([], cur)


def test_retract_event_appears_once(seeded: tuple[FixtureDB, int, int]) -> None:
    fx, chat, first = seeded
    start = fx.db.initial_cursor()
    b.set_date_retracted(fx.writer, first, RETRACT_A, clear_text=True)
    events, cur = poll_once(fx.db, start, include_retractions=True)
    if not _has_retract(fx):
        assert events == [] and cur == start
        return
    assert _kinds(events) == ["retracted"]
    assert events[0].message.rowid == first
    assert events[0].message.date_retracted == RETRACT_A  # raw on the model
    assert events[0].message.text is None  # the row as it is now
    assert events[0].cursor == cur == Cursor(rowid=first, edit_mark=0, retract_mark=RETRACT_A)
    assert isinstance(cur.retract_mark, int)
    # the same unsend is not reported twice; a later one is
    assert poll_once(fx.db, cur, include_retractions=True) == ([], cur)
    assert poll_once(fx.db, Cursor.from_json(cur.to_json()), include_retractions=True) == ([], cur)
    b.set_date_retracted(fx.writer, first, RETRACT_B)
    events, cur2 = poll_once(fx.db, cur, include_retractions=True)
    assert _kinds(events) == ["retracted"] and cur2.retract_mark == RETRACT_B


def test_order_within_a_round_is_new_edited_retracted(
    seeded: tuple[FixtureDB, int, int],
) -> None:
    fx, chat, first = seeded
    second = b.add_message(fx.writer, chat, text="second", handle=PHONE)
    start = fx.db.initial_cursor()
    # wall-clock order is the reverse of the event order: unsend, edit, then insert
    b.set_date_retracted(fx.writer, first, RETRACT_A, clear_text=True)
    b.set_date_edited(fx.writer, second, EDIT_A)
    new = b.add_message(fx.writer, chat, text="fresh")
    events, cur = poll_once(fx.db, start, include_retractions=True)
    has_edit = fx.db.schema.has("message", "date_edited")
    expected = [("new", new)]
    if has_edit:
        expected.append(("edited", second))
    if _has_retract(fx):
        expected.append(("retracted", first))
    assert [(e.kind, e.message.rowid) for e in events] == expected
    assert events[-1].cursor == cur
    assert cur == Cursor(
        rowid=new,
        edit_mark=EDIT_A if has_edit else 0,
        retract_mark=RETRACT_A if _has_retract(fx) else 0,
    )
    # a row that is new, edited and unsent since the marks appears three times
    b.set_date_edited(fx.writer, new, EDIT_B)
    b.set_date_retracted(fx.writer, new, RETRACT_B)
    events, cur2 = poll_once(
        fx.db,
        Cursor(rowid=second, edit_mark=EDIT_A, retract_mark=RETRACT_A),
        include_retractions=True,
    )
    kinds = ["new"] + (["edited"] if has_edit else []) + (["retracted"] if _has_retract(fx) else [])
    assert _kinds(events) == kinds and set(_rowids(events)) == {new}
    assert cur2 == Cursor(
        rowid=new,
        edit_mark=EDIT_B if has_edit else EDIT_A,
        retract_mark=RETRACT_B if _has_retract(fx) else RETRACT_A,
    )


def test_event_cursors_new_edited_retracted(seeded: tuple[FixtureDB, int, int]) -> None:
    """Per-event cursors: new -> own rowid + both starting marks; edited -> final
    rowid + running edit mark + starting retract mark; retracted -> final rowid +
    final edit mark + running retract mark; the last equals the round's."""
    fx, chat, first = seeded
    second = b.add_message(fx.writer, chat, text="second", handle=PHONE)
    start = Cursor(rowid=second, edit_mark=0, retract_mark=0)
    b.set_date_edited(fx.writer, first, EDIT_A)
    b.set_date_edited(fx.writer, second, EDIT_B)
    b.set_date_retracted(fx.writer, second, RETRACT_A)
    b.set_date_retracted(fx.writer, first, RETRACT_B)
    new = b.add_message(fx.writer, chat, text="fresh", handle=PHONE)
    events, cur = poll_once(fx.db, start, include_retractions=True)
    if not _has_retract(fx):
        # no column: no "retracted" events, the retract mark stays put, the rest is 0.1
        assert "retracted" not in _kinds(events) and cur.retract_mark == 0
        assert all(e.cursor.retract_mark == 0 for e in events)
        assert events[-1].cursor == cur
        return
    assert [(e.kind, e.message.rowid) for e in events] == [
        ("new", new),
        ("edited", first),
        ("edited", second),
        ("retracted", second),
        ("retracted", first),
    ]
    assert [e.cursor for e in events] == [
        Cursor(rowid=new, edit_mark=0, retract_mark=0),
        Cursor(rowid=new, edit_mark=EDIT_A, retract_mark=0),
        Cursor(rowid=new, edit_mark=EDIT_B, retract_mark=0),
        Cursor(rowid=new, edit_mark=EDIT_B, retract_mark=RETRACT_A),
        Cursor(rowid=new, edit_mark=EDIT_B, retract_mark=RETRACT_B),
    ]
    assert events[-1].cursor == cur
    # a crash after the last edit replays exactly the unsends
    again, cur2 = poll_once(fx.db, events[2].cursor, include_retractions=True)
    assert [(e.kind, e.message.rowid) for e in again] == [
        ("retracted", second),
        ("retracted", first),
    ]
    assert cur2 == cur
    again, cur3 = poll_once(fx.db, events[3].cursor, include_retractions=True)
    assert [(e.kind, e.message.rowid) for e in again] == [("retracted", first)] and cur3 == cur
    assert poll_once(fx.db, events[-1].cursor, include_retractions=True) == ([], cur)


def test_retract_mark_never_regresses(seeded: tuple[FixtureDB, int, int]) -> None:
    fx, _chat, first = seeded
    b.set_date_retracted(fx.writer, first, RETRACT_A)
    ahead = Cursor(rowid=first, retract_mark=RETRACT_B)  # mark already past the only unsend
    events, cur = poll_once(fx.db, ahead, include_retractions=True)
    assert events == [] and cur == ahead
    # equal to the newest unsend is kept as is too (strict >)
    at = Cursor(rowid=first, retract_mark=RETRACT_A)
    assert poll_once(fx.db, at, include_retractions=True) == ([], at)


def test_tied_date_retracted_checkpoint_replays_the_tie(
    seeded: tuple[FixtureDB, int, int],
) -> None:
    fx, chat, m1 = seeded
    m2 = b.add_message(fx.writer, chat, text="two", handle=PHONE)
    start = Cursor(rowid=m2)
    b.set_date_retracted(fx.writer, m1, RETRACT_A)
    b.set_date_retracted(fx.writer, m2, RETRACT_A)
    events, cur = poll_once(fx.db, start, include_retractions=True)
    if not _has_retract(fx):
        assert events == [] and cur == start
        return
    assert _kinds(events) == ["retracted", "retracted"]
    assert {e.message.rowid for e in events} == {m1, m2}
    # the tie's first member keeps the previous mark; only its last member passes it
    assert events[0].cursor == Cursor(rowid=m2, edit_mark=0, retract_mark=0)
    assert events[1].cursor == Cursor(rowid=m2, edit_mark=0, retract_mark=RETRACT_A) == cur
    again, cur2 = poll_once(fx.db, events[0].cursor, include_retractions=True)
    assert {e.message.rowid for e in again} == {m1, m2} and cur2 == cur
    assert poll_once(fx.db, events[1].cursor, include_retractions=True) == ([], cur)


def test_every_checkpoint_resumes_without_loss_all_three_kinds(
    seeded: tuple[FixtureDB, int, int],
) -> None:
    """Persist ``event.cursor`` after each event and crash there: the replay is a
    suffix of the round covering every unhandled event, and any re-delivered
    handled events are exactly the tied siblings of the last one."""
    fx, chat, first = seeded
    a = b.add_message(fx.writer, chat, text="a")
    bb = b.add_message(fx.writer, chat, text="b")
    c = b.add_message(fx.writer, chat, text="c")
    b.set_date_edited(fx.writer, first, EDIT_A)
    b.set_date_edited(fx.writer, a, EDIT_A)  # tie
    b.set_date_edited(fx.writer, bb, EDIT_B)
    b.set_date_retracted(fx.writer, c, RETRACT_A)
    b.set_date_retracted(fx.writer, a, RETRACT_B)
    b.set_date_retracted(fx.writer, first, RETRACT_B)  # tie
    events, cur = poll_once(fx.db, Cursor(rowid=first), include_retractions=True)
    keys = [(e.kind, e.message.rowid) for e in events]
    assert keys[:3] == [("new", a), ("new", bb), ("new", c)]
    n_edit = 3 if fx.db.schema.has("message", "date_edited") else 0
    n_ret = 3 if _has_retract(fx) else 0
    assert len(keys) == 3 + n_edit + n_ret
    if n_edit:
        assert sorted(keys[3:5]) == sorted([("edited", first), ("edited", a)])
        assert keys[5] == ("edited", bb)
    if n_ret:
        assert keys[3 + n_edit] == ("retracted", c)
        assert sorted(keys[4 + n_edit :]) == sorted([("retracted", a), ("retracted", first)])

    def mark_of(e: Event) -> tuple[str, int] | None:
        if e.kind == "edited":
            return ("edited", e.message.date_edited or 0)
        if e.kind == "retracted":
            return ("retracted", e.message.date_retracted or 0)
        return None

    marks = [mark_of(e) for e in events]
    for k, ev in enumerate(events):
        resumed = Cursor.from_json(ev.cursor.to_json())
        again, cur2 = poll_once(fx.db, resumed, include_retractions=True)
        rest = [(e.kind, e.message.rowid) for e in again]
        assert cur2 == cur
        j = len(keys) - len(rest)
        assert rest == keys[j:], (k, rest)  # a suffix of the round ...
        assert j <= k + 1, (k, rest)  # ... that never skips an unhandled event ...
        # ... and re-delivers only tied siblings of event k
        assert all(marks[i] == marks[k] for i in range(j, k + 1)), (k, rest)
    assert poll_once(fx.db, events[-1].cursor, include_retractions=True) == ([], cur)


def test_include_retractions_false_skips_unsends(seeded: tuple[FixtureDB, int, int]) -> None:
    fx, chat, first = seeded
    start = fx.db.initial_cursor()
    b.set_date_retracted(fx.writer, first, RETRACT_A)
    b.set_date_edited(fx.writer, first, EDIT_A)
    new = b.add_message(fx.writer, chat, text="fresh")
    events, cur = poll_once(fx.db, start, include_retractions=False)
    assert "retracted" not in _kinds(events) and _rowids(events)[0] == new
    assert cur.retract_mark == 0 and cur.rowid == new
    # the unsend is not lost: it is delivered once the flag is switched on
    events, cur2 = poll_once(fx.db, cur, include_retractions=True)
    assert _kinds(events) == (["retracted"] if _has_retract(fx) else [])
    # ChatDB.poll forwards the flag
    assert fx.db.poll(start, include_retractions=False) == poll_once(
        fx.db, start, include_retractions=False
    )
    # both flags off: new only
    events, _ = poll_once(fx.db, start, include_edits=False, include_retractions=False)
    assert _kinds(events) == ["new"]


def test_schema_without_date_retracted_issues_no_query(
    seeded: tuple[FixtureDB, int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    fx, _chat, first = seeded
    b.set_date_retracted(fx.writer, first, RETRACT_A)
    original = Schema.has

    def no_date_retracted(self: Schema, table: str, column: str) -> bool:
        if (table, column) == ("message", "date_retracted"):
            return False
        return original(self, table, column)

    monkeypatch.setattr(Schema, "has", no_date_retracted)
    fx.db.refresh_schema()
    assert fx.db.max_date_retracted() == 0
    assert fx.db.initial_cursor() == Cursor(rowid=first)
    start = Cursor(rowid=first)
    stmts: list[str] = []
    with fx.db.connection() as conn:
        conn.set_trace_callback(stmts.append)
        events, cur = poll_once(fx.db, start, include_retractions=True)
        conn.set_trace_callback(None)
    assert events == [] and cur == start
    # the SELECT aliases ``NULL AS date_retracted``; the tail's WHERE must never run
    assert not any("date_retracted >" in s for s in stmts)
    assert not any("MAX(date_retracted)" in s for s in stmts)


def test_busy_round_keeps_retract_mark(
    seeded: tuple[FixtureDB, int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from imessage_chatdb import polling as polling_mod
    from imessage_chatdb.errors import ChatDBBusy

    fx, _chat, first = seeded
    b.set_date_retracted(fx.writer, first, RETRACT_A)
    start = Cursor(rowid=first, edit_mark=EDIT_A, retract_mark=0)

    def busy(*_a: object, **_k: object) -> object:
        raise ChatDBBusy("database is locked")

    monkeypatch.setattr(polling_mod, "messages_retracted_after", busy)
    assert poll_once(fx.db, start, include_retractions=True) == ([], start)


# ---------------------------------------------------------------------------
# watch / run_watch
# ---------------------------------------------------------------------------


def test_watch_yields_retracted_with_cursor(seeded: tuple[FixtureDB, int, int]) -> None:
    fx, chat, first = seeded
    stop = threading.Event()
    sleeps = 0
    added: list[int] = []

    def fake_sleep(_s: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            added.append(b.add_message(fx.writer, chat, text="r1"))
        elif sleeps == 2:
            b.set_date_retracted(fx.writer, first, RETRACT_A, clear_text=True)
        else:
            stop.set()

    got = list(
        fx.db.watch(Cursor(rowid=first), stop=stop, include_retractions=True, sleep=fake_sleep)
    )
    if not _has_retract(fx):
        assert _kinds(got) == ["new"]
        return
    assert _kinds(got) == ["new", "retracted"]
    assert [e.cursor for e in got] == [
        Cursor(rowid=added[0], edit_mark=0, retract_mark=0),
        Cursor(rowid=added[0], edit_mark=0, retract_mark=RETRACT_A),
    ]
    assert got[1].message.rowid == first and got[1].message.text is None
    resumed = Cursor.from_json(got[-1].cursor.to_json())
    assert poll_once(fx.db, resumed, include_retractions=True) == ([], got[-1].cursor)
    # include_retractions=False on the generator
    stop.clear()
    b.set_date_retracted(fx.writer, first, RETRACT_B)
    gen = watch(
        fx.db, got[-1].cursor, stop=stop, include_retractions=False, sleep=lambda _s: stop.set()
    )
    assert list(gen) == []


def test_run_watch_on_cursor_after_an_unsend(seeded: tuple[FixtureDB, int, int]) -> None:
    fx, chat, first = seeded
    stop = threading.Event()
    events: list[Event] = []
    cursors: list[Cursor] = []
    saved: list[dict[str, int]] = []
    rounds = 0
    added: list[int] = []

    def fake_sleep(_s: float) -> None:
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            added.append(b.add_message(fx.writer, chat, text="r1"))
        elif rounds == 2:
            pass  # idle round: no advance, no on_cursor
        elif rounds == 3:
            b.set_date_retracted(fx.writer, first, RETRACT_A, clear_text=True)
        else:
            stop.set()

    def on_cursor(cur: Cursor) -> None:
        cursors.append(cur)
        saved.append(cur.to_json())

    run_watch(
        fx.db,
        events.append,
        on_cursor=on_cursor,
        cursor=Cursor(rowid=first),
        stop=stop,
        include_retractions=True,
        sleep=fake_sleep,
    )
    assert rounds == 4
    if not _has_retract(fx):
        assert _kinds(events) == ["new"] and cursors == [Cursor(rowid=added[0])]
        return
    assert _kinds(events) == ["new", "retracted"]
    assert _rowids(events) == [added[0], first]
    # round 1 (idle), round 2 (new), round 3 (idle), round 4 (retracted): two advances
    assert cursors == [
        Cursor(rowid=added[0], edit_mark=0, retract_mark=0),
        Cursor(rowid=added[0], edit_mark=0, retract_mark=RETRACT_A),
    ]
    assert cursors[-1] == events[-1].cursor
    assert saved[-1] == {"rowid": added[0], "edit_mark": 0, "retract_mark": RETRACT_A}
    # resuming from the persisted cursor replays nothing
    resumed = Cursor.from_json(saved[-1])
    assert poll_once(fx.db, resumed, include_retractions=True) == ([], cursors[-1])
    # run_watch forwards include_retractions
    stop.clear()
    b.set_date_retracted(fx.writer, added[0], RETRACT_B)
    seen: list[Event] = []
    run_watch(
        fx.db,
        seen.append,
        cursor=cursors[-1],
        stop=stop,
        include_retractions=False,
        sleep=lambda _s: stop.set(),
    )
    assert seen == []


def test_signatures_carry_include_retractions() -> None:
    for fn in (poll_once, watch, run_watch, ChatDB.poll, ChatDB.watch):
        p = inspect.signature(fn).parameters["include_retractions"]
        assert p.default is False and p.kind is inspect.Parameter.KEYWORD_ONLY, fn


def test_default_stream_is_the_0_1_stream(seeded: tuple[FixtureDB, int, int]) -> None:
    """0.2.0 is additive: without ``include_retractions=True`` a consumer sees
    only ``"new"`` and ``"edited"`` events, exactly as in 0.1, and the retract
    mark it never asked for stays put (so opting in later replays the unsend)."""
    fx, chat, first = seeded
    start = fx.db.initial_cursor()
    b.set_date_retracted(fx.writer, first, RETRACT_A, clear_text=True)
    b.set_date_edited(fx.writer, first, EDIT_A)
    new = b.add_message(fx.writer, chat, text="fresh", handle=PHONE)
    has_edit = fx.db.schema.has("message", "date_edited")
    expected = ["new"] + (["edited"] if has_edit else [])
    events, cur = poll_once(fx.db, start)
    assert _kinds(events) == expected
    assert cur == Cursor(
        rowid=new, edit_mark=EDIT_A if has_edit else 0, retract_mark=start.retract_mark
    )
    assert all(e.cursor.retract_mark == start.retract_mark for e in events)
    assert fx.db.poll(start) == (events, cur)
    # the consumer shape 0.1 documented: every non-"new" event is an edit
    for e in events:
        if e.kind != "new":
            assert e.kind == "edited"
    # watch / run_watch / ChatDB.watch with no flag: the same two kinds
    stop = threading.Event()
    got = list(fx.db.watch(start, stop=stop, sleep=lambda _s: stop.set()))
    assert _kinds(got) == expected
    stop.clear()
    seen: list[Event] = []
    run_watch(fx.db, seen.append, cursor=start, stop=stop, sleep=lambda _s: stop.set())
    assert _kinds(seen) == expected
    stop.clear()
    assert _kinds(list(watch(fx.db, start, stop=stop, sleep=lambda _s: stop.set()))) == expected
    # opting in afterwards delivers the unsend the default stream skipped
    events, cur2 = poll_once(fx.db, cur, include_retractions=True)
    assert _kinds(events) == (["retracted"] if _has_retract(fx) else [])
    assert cur2.retract_mark == (RETRACT_A if _has_retract(fx) else 0)
