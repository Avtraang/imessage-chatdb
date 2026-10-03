"""Tests for ``polling.py`` (DESIGN.md 4.6, 8.4 watch, T8).

Synthetic databases only.  ``poll_once`` is exercised directly and through
``ChatDB.poll``; ``watch``/``run_watch`` run with an injected ``sleep`` and a
stop ``Event`` so no test waits on a clock.
"""

from __future__ import annotations

import dataclasses
import importlib
import sqlite3
import threading
from collections.abc import Iterator
from typing import Any

import pytest

import imessage_chatdb
from imessage_chatdb.db import ChatDB
from imessage_chatdb.errors import ChatDBBusy, CursorAhead
from imessage_chatdb.polling import Cursor, Event, poll_once, run_watch, watch
from imessage_chatdb.schema import Schema
from tests.conftest import FixtureDB, MakeDB, Pristine, sha256_of
from tests.fixtures import builders as b

# The polling code lives in ``imessage_chatdb.polling``; the package re-exports
# its names, so ``imessage_chatdb.watch`` is the generator function itself.
polling_mod = importlib.import_module("imessage_chatdb.polling")

PHONE = "+15550001234"
CHAT = "any;-;+15550001234"

EDIT_A = b.BASE_DATE_NS + 300 * b.DATE_STEP_NS
EDIT_B = b.BASE_DATE_NS + 500 * b.DATE_STEP_NS


def _kinds(events: list[Event]) -> list[str]:
    return [e.kind for e in events]


def _rowids(events: list[Event]) -> list[int]:
    return [e.message.rowid for e in events]


@pytest.fixture
def seeded(fixture_db: FixtureDB) -> tuple[FixtureDB, int, int]:
    """A chat with one existing message; returns ``(fixture, chat_rowid, first_msg_rowid)``."""
    chat = b.add_chat(fixture_db.writer, CHAT, handles=[PHONE])
    first = b.add_message(fixture_db.writer, chat, text="existing", handle=PHONE)
    return fixture_db, chat, first


# ---------------------------------------------------------------------------
# Cursor / Event
# ---------------------------------------------------------------------------


def test_cursor_defaults_frozen_and_json_round_trip() -> None:
    c = Cursor()
    assert (c.rowid, c.edit_mark) == (0, 0)
    assert dataclasses.is_dataclass(c)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.rowid = 5  # type: ignore[misc]
    assert not hasattr(c, "__dict__")  # slots

    c = Cursor(rowid=42, edit_mark=EDIT_A)
    d = c.to_json()
    assert d == {"rowid": 42, "edit_mark": EDIT_A}
    assert Cursor.from_json(d) == c
    assert isinstance(Cursor.from_json(d).edit_mark, int)
    # tolerant of missing / null keys (a fresh state file)
    assert Cursor.from_json({}) == Cursor()
    assert Cursor.from_json({"rowid": None, "edit_mark": None}) == Cursor()
    assert Cursor.from_json({"rowid": "7"}) == Cursor(rowid=7)


def test_event_is_frozen_slotted(seeded: tuple[FixtureDB, int, int]) -> None:
    fx, _chat, first = seeded
    msg = fx.db.message(first)
    assert msg is not None
    cur = Cursor(rowid=first, edit_mark=0)
    ev = Event("new", msg, cur)
    assert ev.kind == "new" and ev.message is msg and ev.cursor == cur
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.kind = "edited"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.cursor = Cursor()  # type: ignore[misc]
    assert not hasattr(ev, "__dict__")
    assert [f.name for f in dataclasses.fields(Event)] == ["kind", "message", "cursor"]


def test_public_names_exported() -> None:
    assert imessage_chatdb.Cursor is Cursor
    assert imessage_chatdb.Event is Event
    assert imessage_chatdb.poll_once is poll_once
    assert imessage_chatdb.run_watch is run_watch
    # the generator function is a package export; the module is ``polling``
    assert imessage_chatdb.watch is watch
    assert callable(imessage_chatdb.watch)
    assert imessage_chatdb.polling is polling_mod
    assert polling_mod.watch is watch
    assert polling_mod.poll_once is poll_once and polling_mod.Cursor is Cursor
    assert {"Cursor", "Event", "poll_once", "watch", "run_watch"} <= set(imessage_chatdb.__all__)
    assert set(polling_mod.__all__) == {"Cursor", "Event", "poll_once", "watch", "run_watch"}
    assert ChatDB.watch.__doc__ is not None
    assert "imessage_chatdb.polling.watch" in ChatDB.watch.__doc__
    # no shim is left behind
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("imessage_chatdb.watch")


# ---------------------------------------------------------------------------
# Event.cursor: the cursor to persist once that event has been handled
# ---------------------------------------------------------------------------


def test_new_events_carry_their_own_rowid_and_the_starting_mark(
    seeded: tuple[FixtureDB, int, int],
) -> None:
    fx, chat, first = seeded
    start = Cursor(rowid=first, edit_mark=EDIT_A)  # a non-zero starting mark
    a = b.add_message(fx.writer, chat, text="one", handle=PHONE)
    bb = b.add_message(fx.writer, chat, text="two", is_from_me=1)
    c = b.add_message(fx.writer, chat, text="three", handle=PHONE)
    events, cur = poll_once(fx.db, start)
    assert [e.cursor for e in events] == [
        Cursor(rowid=a, edit_mark=EDIT_A),
        Cursor(rowid=bb, edit_mark=EDIT_A),
        Cursor(rowid=c, edit_mark=EDIT_A),
    ]
    assert events[-1].cursor == cur


def test_edited_events_carry_final_rowid_and_running_max_mark(
    seeded: tuple[FixtureDB, int, int],
) -> None:
    fx, chat, first = seeded
    second = b.add_message(fx.writer, chat, text="second", handle=PHONE)
    start = Cursor(rowid=second, edit_mark=0)
    # two edits with distinct marks: EDIT_A on ``second``, EDIT_B (later) on ``first``
    b.set_date_edited(fx.writer, second, EDIT_A)
    b.set_date_edited(fx.writer, first, EDIT_B)
    new = b.add_message(fx.writer, chat, text="fresh", handle=PHONE)
    events, cur = poll_once(fx.db, start)
    assert _kinds(events) == ["new", "edited", "edited"]
    assert _rowids(events) == [new, second, first]  # edits ascending by date_edited
    assert [e.cursor for e in events] == [
        Cursor(rowid=new, edit_mark=0),  # new: own rowid, the round's starting mark
        Cursor(rowid=new, edit_mark=EDIT_A),  # edited: the round's final rowid, running max
        Cursor(rowid=new, edit_mark=EDIT_B),
    ]
    assert events[-1].cursor == cur == Cursor(rowid=new, edit_mark=EDIT_B)


def test_edited_event_mark_is_max_of_previous_and_its_own(
    seeded: tuple[FixtureDB, int, int],
) -> None:
    fx, _chat, first = seeded
    b.set_date_edited(fx.writer, first, EDIT_A)
    # the starting mark is just below the edit: the event's mark is the edit's
    events, cur = poll_once(fx.db, Cursor(rowid=first, edit_mark=EDIT_A - 1))
    assert _kinds(events) == ["edited"]
    assert events[0].cursor == Cursor(rowid=first, edit_mark=EDIT_A) == cur
    assert events[0].message.date_edited == EDIT_A


def test_last_event_cursor_equals_round_cursor_in_every_shape(
    seeded: tuple[FixtureDB, int, int],
) -> None:
    fx, chat, first = seeded
    # only new
    n1 = b.add_message(fx.writer, chat, text="n1")
    events, cur = poll_once(fx.db, Cursor(rowid=first))
    assert _kinds(events) == ["new"] and events[-1].cursor == cur
    # only edited
    b.set_date_edited(fx.writer, first, EDIT_A)
    events, cur = poll_once(fx.db, cur)
    assert _kinds(events) == ["edited"] and events[-1].cursor == cur
    # new + edited, include_edits=False
    n2 = b.add_message(fx.writer, chat, text="n2")
    b.set_date_edited(fx.writer, n1, EDIT_B)
    events, cur2 = poll_once(fx.db, cur, include_edits=False)
    assert _rowids(events) == [n2] and events[-1].cursor == cur2 == Cursor(n2, EDIT_A)
    # the same round with edits
    events, cur3 = poll_once(fx.db, cur)
    assert _kinds(events) == ["new", "edited"] and events[-1].cursor == cur3
    assert cur3 == Cursor(rowid=n2, edit_mark=EDIT_B)


def test_resuming_from_a_mid_batch_event_cursor_replays_only_the_rest(
    seeded: tuple[FixtureDB, int, int],
) -> None:
    """A crash after handling event k and persisting its cursor loses nothing."""
    fx, chat, first = seeded
    a = b.add_message(fx.writer, chat, text="a")
    bb = b.add_message(fx.writer, chat, text="b")
    c = b.add_message(fx.writer, chat, text="c")
    b.set_date_edited(fx.writer, first, EDIT_A)
    b.set_date_edited(fx.writer, a, EDIT_B)
    events, cur = poll_once(fx.db, Cursor(rowid=first))
    assert _kinds(events) == ["new", "new", "new", "edited", "edited"]
    # persisted after the second "new" event: the third new and both edits come back
    again, cur2 = poll_once(fx.db, Cursor.from_json(events[1].cursor.to_json()))
    assert [(e.kind, e.message.rowid) for e in again] == [
        ("new", c), ("edited", first), ("edited", a)
    ]
    assert cur2 == cur
    # persisted after the first "edited" event: only the second edit comes back
    again, cur3 = poll_once(fx.db, events[3].cursor)
    assert [(e.kind, e.message.rowid) for e in again] == [("edited", a)]
    assert cur3 == cur
    # persisted after the last event: nothing comes back
    assert poll_once(fx.db, events[-1].cursor) == ([], cur)
    assert bb < c


def test_tied_date_edited_checkpoint_replays_the_tie_instead_of_losing_it(
    seeded: tuple[FixtureDB, int, int],
) -> None:
    """Two rows edited with the same raw date_edited: a checkpoint on the first
    must not lose the second (the resume query is strict, ``> mark``)."""
    fx, chat, m1 = seeded
    m2 = b.add_message(fx.writer, chat, text="two", handle=PHONE)
    start = Cursor(rowid=m2, edit_mark=0)
    b.set_date_edited(fx.writer, m1, EDIT_A)
    b.set_date_edited(fx.writer, m2, EDIT_A)
    events, cur = poll_once(fx.db, start)
    if not fx.db.schema.has("message", "date_edited"):
        assert events == [] and cur == start
        return
    assert _kinds(events) == ["edited", "edited"]
    assert {e.message.rowid for e in events} == {m1, m2}
    # the tie's first member keeps the previous mark; only its last member passes it
    assert events[0].cursor == Cursor(rowid=m2, edit_mark=0)
    assert events[1].cursor == Cursor(rowid=m2, edit_mark=EDIT_A) == cur
    # persisted after the first tied event, crashed: the whole tie comes back
    again, cur2 = poll_once(fx.db, events[0].cursor)
    assert {e.message.rowid for e in again} == {m1, m2} and cur2 == cur
    # persisted after the second: nothing comes back
    assert poll_once(fx.db, events[1].cursor) == ([], cur)


def test_every_checkpoint_resumes_without_loss_with_ties(
    seeded: tuple[FixtureDB, int, int],
) -> None:
    """Persist ``event.cursor`` after each event and crash there: the replay is
    always a suffix of the round that covers every unhandled event, and any
    re-delivered handled events are exactly the tied siblings of the last one."""
    fx, chat, first = seeded
    a = b.add_message(fx.writer, chat, text="a")
    bb = b.add_message(fx.writer, chat, text="b")
    c = b.add_message(fx.writer, chat, text="c")
    b.set_date_edited(fx.writer, first, EDIT_A)
    b.set_date_edited(fx.writer, a, EDIT_A)  # tie with ``first``
    b.set_date_edited(fx.writer, bb, EDIT_B)
    b.set_date_edited(fx.writer, c, EDIT_B)  # tie with ``bb``
    events, cur = poll_once(fx.db, Cursor(rowid=first))
    keys = [(e.kind, e.message.rowid) for e in events]
    if not fx.db.schema.has("message", "date_edited"):
        assert keys == [("new", a), ("new", bb), ("new", c)]
    else:
        assert keys[:3] == [("new", a), ("new", bb), ("new", c)]
        assert sorted(keys[3:5]) == sorted([("edited", first), ("edited", a)])
        assert sorted(keys[5:]) == sorted([("edited", bb), ("edited", c)])
    marks = [e.message.date_edited or 0 if e.kind == "edited" else None for e in events]
    for k, ev in enumerate(events):
        again, cur2 = poll_once(fx.db, Cursor.from_json(ev.cursor.to_json()))
        rest = [(e.kind, e.message.rowid) for e in again]
        assert cur2 == cur
        # a suffix of the round ...
        j = len(keys) - len(rest)
        assert rest == keys[j:], (k, rest)
        # ... that never skips an unhandled event ...
        assert j <= k + 1, (k, rest)
        # ... and re-delivers only tied siblings of event k (at-least-once, grouped)
        assert all(marks[i] == marks[k] for i in range(j, k + 1)), (k, rest)
    assert poll_once(fx.db, events[-1].cursor) == ([], cur)


def test_watch_events_carry_cursors_across_rounds(seeded: tuple[FixtureDB, int, int]) -> None:
    fx, chat, first = seeded
    stop = threading.Event()
    sleeps = 0
    added: list[int] = []

    def fake_sleep(_s: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            added.append(b.add_message(fx.writer, chat, text="r1"))
            added.append(b.add_message(fx.writer, chat, text="r1b"))
        elif sleeps == 2:
            b.set_date_edited(fx.writer, first, EDIT_A)
        else:
            stop.set()

    got = list(fx.db.watch(Cursor(rowid=first), stop=stop, sleep=fake_sleep))
    assert _kinds(got) == ["new", "new", "edited"]
    assert [e.cursor for e in got] == [
        Cursor(rowid=added[0], edit_mark=0),
        Cursor(rowid=added[1], edit_mark=0),
        Cursor(rowid=added[1], edit_mark=EDIT_A),
    ]
    # the README recipe: resume from the last persisted event cursor
    assert poll_once(fx.db, Cursor.from_json(got[-1].cursor.to_json())) == ([], got[-1].cursor)


def test_run_watch_events_carry_cursors_and_on_cursor_matches_last(
    seeded: tuple[FixtureDB, int, int],
) -> None:
    fx, chat, first = seeded
    stop = threading.Event()
    a = b.add_message(fx.writer, chat, text="a")
    c = b.add_message(fx.writer, chat, text="c")
    events: list[Event] = []
    cursors: list[Cursor] = []
    run_watch(
        fx.db,
        events.append,
        on_cursor=cursors.append,
        cursor=Cursor(rowid=first),
        stop=stop,
        sleep=lambda _s: stop.set(),
    )
    assert [e.cursor for e in events] == [Cursor(rowid=a), Cursor(rowid=c)]
    assert cursors == [events[-1].cursor]


# ---------------------------------------------------------------------------
# poll_once
# ---------------------------------------------------------------------------


def test_three_inserts_three_new_events_in_order(seeded: tuple[FixtureDB, int, int]) -> None:
    fx, chat, first = seeded
    db: ChatDB = fx.db
    start = db.initial_cursor()
    assert start == Cursor(rowid=first, edit_mark=0)

    a = b.add_message(fx.writer, chat, text="one", handle=PHONE)
    bb = b.add_message(fx.writer, chat, text="two", is_from_me=1)
    c = b.add_message(fx.writer, chat, text="three", handle=PHONE)

    events, cur = poll_once(db, start)
    assert _kinds(events) == ["new", "new", "new"]
    assert _rowids(events) == [a, bb, c]
    assert [e.message.text for e in events] == ["one", "two", "three"]
    assert all(e.message.enriched for e in events)
    assert cur == Cursor(rowid=c, edit_mark=0)
    assert isinstance(cur, Cursor)
    # the existing row was never replayed
    assert first not in _rowids(events)


def test_idle_poll_yields_nothing_and_keeps_cursor(seeded: tuple[FixtureDB, int, int]) -> None:
    fx, _chat, _first = seeded
    start = fx.db.initial_cursor()
    events, cur = poll_once(fx.db, start)
    assert events == [] and cur == start
    # and again
    assert poll_once(fx.db, cur) == ([], cur)


def test_rowid_advances_only_to_last_delivered(seeded: tuple[FixtureDB, int, int]) -> None:
    fx, chat, first = seeded
    # an orphan (no chat_message_join row) raises MAX(ROWID) without being delivered
    delivered = b.add_message(fx.writer, chat, text="joined")
    orphan = b.add_message(fx.writer, chat, text="orphan", join=False)
    assert orphan > delivered
    events, cur = poll_once(fx.db, Cursor(rowid=first))
    assert _rowids(events) == [delivered]
    assert cur.rowid == delivered  # not MAX(ROWID)


def test_edit_bump_yields_edited_event_with_raw_int_mark(
    seeded: tuple[FixtureDB, int, int],
) -> None:
    fx, _chat, first = seeded
    start = fx.db.initial_cursor()
    assert start.edit_mark == 0

    b.set_date_edited(fx.writer, first, EDIT_A)
    events, cur = poll_once(fx.db, start)
    assert _kinds(events) == ["edited"]
    assert events[0].message.rowid == first
    assert events[0].message.date_edited == EDIT_A  # raw on the model
    assert cur.edit_mark == EDIT_A and isinstance(cur.edit_mark, int)
    assert cur.rowid == start.rowid

    # the same edit is not reported twice; a later bump is
    assert poll_once(fx.db, cur) == ([], cur)
    b.set_date_edited(fx.writer, first, EDIT_B)
    events, cur2 = poll_once(fx.db, cur)
    assert _kinds(events) == ["edited"] and cur2.edit_mark == EDIT_B


def test_edit_mark_never_regresses(seeded: tuple[FixtureDB, int, int]) -> None:
    fx, _chat, first = seeded
    b.set_date_edited(fx.writer, first, EDIT_A)
    ahead = Cursor(rowid=first, edit_mark=EDIT_B)  # mark already past the only edit
    events, cur = poll_once(fx.db, ahead)
    assert events == [] and cur == ahead


def test_new_precede_edited_within_a_round(seeded: tuple[FixtureDB, int, int]) -> None:
    fx, chat, first = seeded
    start = fx.db.initial_cursor()
    # an edit to an old row, then a new row - the edit is older in wall-clock terms
    b.set_date_edited(fx.writer, first, EDIT_A)
    new = b.add_message(fx.writer, chat, text="fresh")
    events, cur = poll_once(fx.db, start)
    assert _kinds(events) == ["new", "edited"]
    assert _rowids(events) == [new, first]
    assert cur == Cursor(rowid=new, edit_mark=EDIT_A)


def test_include_edits_false_skips_edits(seeded: tuple[FixtureDB, int, int]) -> None:
    fx, chat, first = seeded
    start = fx.db.initial_cursor()
    b.set_date_edited(fx.writer, first, EDIT_A)
    new = b.add_message(fx.writer, chat, text="fresh")
    events, cur = poll_once(fx.db, start, include_edits=False)
    assert _kinds(events) == ["new"] and _rowids(events) == [new]
    assert cur == Cursor(rowid=new, edit_mark=0)
    # ChatDB.poll forwards the flag
    assert fx.db.poll(start, include_edits=False) == (events, cur)


def test_include_edits_respects_schema_without_date_edited(
    seeded: tuple[FixtureDB, int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    fx, _chat, first = seeded
    b.set_date_edited(fx.writer, first, EDIT_A)
    original = Schema.has

    def no_date_edited(self: Schema, table: str, column: str) -> bool:
        if (table, column) == ("message", "date_edited"):
            return False
        return original(self, table, column)

    monkeypatch.setattr(Schema, "has", no_date_edited)
    fx.db.refresh_schema()
    assert fx.db.max_date_edited() == 0
    start = Cursor(rowid=first, edit_mark=0)
    stmts: list[str] = []
    with fx.db.connection() as conn:
        conn.set_trace_callback(stmts.append)
        events, cur = poll_once(fx.db, start, include_edits=True)
        conn.set_trace_callback(None)
    assert events == [] and cur == start
    assert not any("date_edited >" in s for s in stmts)


def test_rebuilt_db_raises_cursor_ahead(seeded: tuple[FixtureDB, int, int]) -> None:
    fx, _chat, first = seeded
    with pytest.raises(CursorAhead) as info:
        poll_once(fx.db, Cursor(rowid=first + 100))
    assert info.value.cursor_rowid == first + 100
    assert info.value.max_rowid == first
    # equal is fine (nothing new), and so is an empty database with cursor 0
    assert poll_once(fx.db, Cursor(rowid=first)) == ([], Cursor(rowid=first))


def test_empty_database_polls_clean(fixture_db: FixtureDB) -> None:
    assert fixture_db.db.initial_cursor() == Cursor()
    assert poll_once(fixture_db.db, Cursor()) == ([], Cursor())


def test_busy_returns_no_events_and_same_cursor_monkeypatched(
    seeded: tuple[FixtureDB, int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    fx, chat, first = seeded
    b.add_message(fx.writer, chat, text="would be new")
    start = Cursor(rowid=first)

    def busy(*_a: Any, **_k: Any) -> Any:
        raise ChatDBBusy("database is locked")

    # busy mid-round (after MAX(ROWID) succeeded)
    monkeypatch.setattr(polling_mod, "messages_after", busy)
    assert poll_once(fx.db, start) == ([], start)
    # busy at open time
    monkeypatch.undo()
    monkeypatch.setattr(ChatDB, "connect", busy)
    assert poll_once(fx.db, start) == ([], start)


def test_busy_returns_no_events_real_lock(make_db: MakeDB) -> None:
    """A real ``BEGIN EXCLUSIVE`` on a rollback-journal database -> ``([], cursor)``."""
    fx = make_db("macos27")
    w = fx.writer
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    first = b.add_message(w, chat, text="existing")
    assert w.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    db = ChatDB(fx.path, timeout=0.05)
    start = Cursor(rowid=first)
    w.execute("BEGIN EXCLUSIVE")
    try:
        assert poll_once(db, start) == ([], start)
    finally:
        w.execute("COMMIT")
    # and once the lock is gone the round works
    new = b.add_message(w, chat, text="after")
    events, cur = poll_once(db, start)
    assert _rowids(events) == [new] and cur.rowid == new


def test_poll_once_uses_one_connection_per_round(
    seeded: tuple[FixtureDB, int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    fx, chat, first = seeded
    b.add_message(fx.writer, chat, text="n")
    b.set_date_edited(fx.writer, first, EDIT_A)
    real = ChatDB.connect
    opened: list[sqlite3.Connection] = []

    def counting(self: ChatDB) -> sqlite3.Connection:
        conn = real(self)
        opened.append(conn)
        return conn

    monkeypatch.setattr(ChatDB, "connect", counting)
    events, _cur = poll_once(fx.db, Cursor(rowid=first))
    assert _kinds(events) == ["new", "edited"]
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")  # closed in finally


def test_poll_facade_matches_poll_once(seeded: tuple[FixtureDB, int, int]) -> None:
    fx, chat, first = seeded
    b.add_message(fx.writer, chat, text="n")
    start = Cursor(rowid=first)
    assert fx.db.poll(start) == poll_once(fx.db, start)


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------


def test_watch_cursor_none_starts_at_now_no_replay(seeded: tuple[FixtureDB, int, int]) -> None:
    fx, chat, _first = seeded
    b.add_message(fx.writer, chat, text="also existing")
    sleeps: list[float] = []
    stop = threading.Event()

    def fake_sleep(s: float) -> None:
        sleeps.append(s)
        stop.set()  # one round only

    got = list(watch(fx.db, None, interval=0.25, stop=stop, sleep=fake_sleep))
    assert got == []
    assert sleeps == [0.25]


def test_watch_yields_events_and_stops(seeded: tuple[FixtureDB, int, int]) -> None:
    fx, chat, first = seeded
    stop = threading.Event()
    sleeps: list[float] = []
    inserted: list[int] = []

    def fake_sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) == 1:
            inserted.append(b.add_message(fx.writer, chat, text="late"))
            b.set_date_edited(fx.writer, first, EDIT_A)
        else:
            stop.set()

    got = list(watch(fx.db, Cursor(rowid=first), interval=1.5, stop=stop, sleep=fake_sleep))
    assert _kinds(got) == ["new", "edited"]
    assert _rowids(got) == [inserted[0], first]
    assert sleeps == [1.5, 1.5]


def test_watch_stop_set_by_consumer_ends_without_sleeping(
    seeded: tuple[FixtureDB, int, int],
) -> None:
    fx, chat, first = seeded
    b.add_message(fx.writer, chat, text="a")
    b.add_message(fx.writer, chat, text="b")
    stop = threading.Event()
    sleeps: list[float] = []
    got: list[Event] = []
    for ev in watch(fx.db, Cursor(rowid=first), stop=stop, sleep=sleeps.append):
        got.append(ev)
        stop.set()
    # both events of the round are delivered (they were already fetched)...
    assert _kinds(got) == ["new", "new"]
    # ...and the generator exits before sleeping
    assert sleeps == []


def test_watch_stop_already_set_polls_nothing(
    seeded: tuple[FixtureDB, int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    fx, _chat, _first = seeded
    stop = threading.Event()
    stop.set()
    calls: list[int] = []
    monkeypatch.setattr(ChatDB, "connect", lambda self: calls.append(1))
    assert list(watch(fx.db, Cursor(), stop=stop, sleep=lambda _s: None)) == []
    assert calls == []


def test_watch_default_sleep_is_time_sleep() -> None:
    import inspect
    import time

    assert inspect.signature(watch).parameters["sleep"].default is time.sleep
    assert inspect.signature(watch).parameters["interval"].default == 2.0
    assert inspect.signature(ChatDB.watch).parameters["sleep"].default is time.sleep


def test_watch_propagates_cursor_ahead(seeded: tuple[FixtureDB, int, int]) -> None:
    fx, _chat, first = seeded
    gen: Iterator[Event] = watch(fx.db, Cursor(rowid=first + 5), sleep=lambda _s: None)
    with pytest.raises(CursorAhead):
        next(gen)


def test_watch_busy_round_is_silent(
    seeded: tuple[FixtureDB, int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    fx, chat, first = seeded
    new = b.add_message(fx.writer, chat, text="n")
    real = ChatDB.connect
    rounds = 0

    def flaky(self: ChatDB) -> sqlite3.Connection:
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            raise ChatDBBusy("database is locked")
        return real(self)

    monkeypatch.setattr(ChatDB, "connect", flaky)
    stop = threading.Event()
    sleeps: list[float] = []

    def fake_sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) == 2:
            stop.set()

    got = list(fx.db.watch(Cursor(rowid=first), stop=stop, sleep=fake_sleep))
    assert _rowids(got) == [new]  # delivered on the second (unlocked) round
    assert sleeps == [2.0, 2.0]


def test_chatdb_watch_delegates(seeded: tuple[FixtureDB, int, int]) -> None:
    fx, chat, first = seeded
    new = b.add_message(fx.writer, chat, text="n")
    stop = threading.Event()
    gen = fx.db.watch(Cursor(rowid=first), stop=stop, sleep=lambda _s: stop.set())
    assert _rowids(list(gen)) == [new]


# ---------------------------------------------------------------------------
# run_watch
# ---------------------------------------------------------------------------


def test_run_watch_on_cursor_after_each_advance(seeded: tuple[FixtureDB, int, int]) -> None:
    fx, chat, first = seeded
    stop = threading.Event()
    events: list[Event] = []
    cursors: list[Cursor] = []
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
            b.set_date_edited(fx.writer, first, EDIT_A)
        else:
            stop.set()

    run_watch(
        fx.db,
        events.append,
        on_cursor=cursors.append,
        cursor=Cursor(rowid=first),
        stop=stop,
        sleep=fake_sleep,
    )
    assert _kinds(events) == ["new", "edited"]
    assert _rowids(events) == [added[0], first]
    # round 1 (idle), round 2 (new), round 3 (idle), round 4 (edited): two advances
    assert cursors == [
        Cursor(rowid=added[0], edit_mark=0),
        Cursor(rowid=added[0], edit_mark=EDIT_A),
    ]
    assert rounds == 4


def test_run_watch_without_on_cursor_and_cursor_none(
    seeded: tuple[FixtureDB, int, int],
) -> None:
    fx, chat, _first = seeded
    stop = threading.Event()
    seen: list[Event] = []
    added: list[int] = []

    def fake_sleep(_s: float) -> None:
        if not added:
            added.append(b.add_message(fx.writer, chat, text="after start"))
        else:
            stop.set()

    run_watch(fx.db, seen.append, stop=stop, sleep=fake_sleep)  # cursor=None -> now
    assert _rowids(seen) == added


def test_run_watch_event_order_and_persistence_round_trip(
    seeded: tuple[FixtureDB, int, int],
) -> None:
    fx, chat, first = seeded
    stop = threading.Event()
    saved: list[dict[str, int]] = []
    a = b.add_message(fx.writer, chat, text="a")
    c = b.add_message(fx.writer, chat, text="c")
    run_watch(
        fx.db,
        lambda _e: None,
        on_cursor=lambda cur: saved.append(cur.to_json()),
        cursor=Cursor(rowid=first),
        stop=stop,
        sleep=lambda _s: stop.set(),
    )
    assert saved == [{"rowid": c, "edit_mark": 0}]
    # resuming from the persisted cursor replays nothing
    assert poll_once(fx.db, Cursor.from_json(saved[0])) == ([], Cursor(rowid=c))
    assert a < c


# ---------------------------------------------------------------------------
# suite-wide read-only guard: polling and watching the pristine database
# ---------------------------------------------------------------------------


def test_poll_and_watch_leave_the_pristine_file_untouched(pristine_db: Pristine) -> None:
    db = ChatDB(pristine_db.path)
    events, cur = poll_once(db, Cursor())
    assert _kinds(events) == ["new"] * pristine_db.message_count
    assert _rowids(events) == list(pristine_db.rowids)
    assert cur == Cursor(pristine_db.rowids[-1], 0)
    # idle round: nothing new, cursor unchanged
    assert poll_once(db, cur) == ([], cur)
    # watch() from "now" sees nothing and exits on the stop event
    stop = threading.Event()
    seen: list[Event] = []

    def fake_sleep(_: float) -> None:
        stop.set()

    for ev in watch(db, None, stop=stop, sleep=fake_sleep):
        seen.append(ev)
    assert seen == []
    assert sha256_of(pristine_db.path) == pristine_db.sha256
