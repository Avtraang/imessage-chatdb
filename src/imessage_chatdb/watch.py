"""Polling for new and edited messages (DESIGN.md section 4.6).

The unit of progress is a frozen :class:`Cursor`: the last ROWID delivered plus
the raw Apple ``date_edited`` high-water mark (an ``int`` that is never
converted).  :func:`poll_once` is the primitive - one round, one connection,
pure data in and out; :func:`watch` and :func:`run_watch` loop over it with an
injectable ``sleep`` and an optional stop :class:`threading.Event`.

The rules of a round are those of the relay's polling loop:
new rows strictly after the cursor, ascending by ROWID; then rows whose
``date_edited`` advanced past the mark, ascending by ``date_edited``; the
ROWID cursor advances only to the last row delivered (at-least-once), the mark
only forward.  Two things are the library's own: a database whose ``MAX(ROWID)``
fell below the cursor raises :class:`CursorAhead` (the relay's generic
``except`` would spin on it), and a locked database yields ``([], cursor)`` so
a caller can simply try again next round.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from .errors import ChatDBBusy, CursorAhead
from .messages import max_rowid, messages_after, messages_edited_after
from .models import Message

if TYPE_CHECKING:
    from .db import ChatDB

__all__ = ["Cursor", "Event", "poll_once", "watch", "run_watch"]

EventKind = Literal["new", "edited"]


@dataclass(frozen=True, slots=True)
class Cursor:
    """Where a watcher is: the last ROWID delivered and the raw edit mark.

    ``edit_mark`` is ``message.date_edited`` as stored (Apple-epoch
    nanoseconds), never converted, so persisting and comparing it is exact.
    ``Cursor()`` (both zero) replays the whole database; use
    ``ChatDB.initial_cursor()`` to start at "now".
    """

    rowid: int = 0
    edit_mark: int = 0

    def to_json(self) -> dict[str, int]:
        """A JSON-ready dict: ``{"rowid": ..., "edit_mark": ...}``."""
        return {"rowid": self.rowid, "edit_mark": self.edit_mark}

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> Cursor:
        """Inverse of :meth:`to_json`; missing or ``None`` keys read as ``0``."""
        return cls(rowid=int(d.get("rowid") or 0), edit_mark=int(d.get("edit_mark") or 0))


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that happened: a ``"new"`` message or an ``"edited"`` one."""

    kind: EventKind
    message: Message


def poll_once(
    db: ChatDB, cursor: Cursor, *, include_edits: bool = True
) -> tuple[list[Event], Cursor]:
    """One polling round on one connection; returns ``(events, new_cursor)``.

    - ``top = MAX(ROWID)``; when ``top < cursor.rowid`` the database was rebuilt
      (or replaced) and :class:`CursorAhead` is raised - the caller decides
      whether to re-initialise at ``top`` or replay from zero.
    - New messages are ``messages_after(cursor.rowid)`` (enriched); the cursor's
      ``rowid`` advances only to the last one delivered, so nothing is skipped
      if the caller crashes mid-batch (at-least-once).
    - Edits are fetched only when ``include_edits`` and the schema has
      ``message.date_edited``; the mark becomes the largest raw value returned
      and never regresses.
    - ``"new"`` events precede ``"edited"`` events within the round.  A row that
      is both new and edited since the mark appears twice, as in the relay.
    - :class:`ChatDBBusy` anywhere in the round -> ``([], cursor)``: no events,
      nothing advanced, safe to retry.
    """
    try:
        with db.connection() as conn:
            top = max_rowid(conn)
            if top < cursor.rowid:
                raise CursorAhead(cursor.rowid, top)
            schema = db.schema
            new = messages_after(conn, schema, cursor.rowid)
            rowid = new[-1].rowid if new else cursor.rowid
            edited: list[Message] = []
            mark = cursor.edit_mark
            if include_edits and schema.has("message", "date_edited"):
                edited, mark = messages_edited_after(conn, schema, cursor.edit_mark)
                if mark < cursor.edit_mark:  # defensive: the mark only moves forward
                    mark = cursor.edit_mark
    except ChatDBBusy:
        return [], cursor
    events: list[Event] = [Event("new", m) for m in new]
    events.extend(Event("edited", m) for m in edited)
    return events, Cursor(rowid=rowid, edit_mark=mark)


def _rounds(
    db: ChatDB,
    cursor: Cursor | None,
    *,
    interval: float,
    stop: threading.Event | None,
    include_edits: bool,
    sleep: Callable[[float], object],
) -> Iterator[tuple[list[Event], Cursor, bool]]:
    """Yield ``(events, cursor, advanced)`` per round until ``stop`` is set.

    The stop flag is checked before every round and again after it (before the
    sleep), so a consumer that sets it while handling events never waits a
    full ``interval`` for the generator to notice.
    """
    cur = db.initial_cursor() if cursor is None else cursor
    while stop is None or not stop.is_set():
        events, new_cur = poll_once(db, cur, include_edits=include_edits)
        advanced = new_cur != cur
        cur = new_cur
        yield events, cur, advanced
        if stop is not None and stop.is_set():
            break
        sleep(interval)


def watch(
    db: ChatDB,
    cursor: Cursor | None = None,
    *,
    interval: float = 2.0,
    stop: threading.Event | None = None,
    include_edits: bool = True,
    sleep: Callable[[float], object] = time.sleep,
) -> Iterator[Event]:
    """Yield :class:`Event` objects forever (or until ``stop`` is set).

    ``cursor=None`` starts at ``db.initial_cursor()`` - "now", with no replay.
    Between rounds the generator calls ``sleep(interval)`` (injectable for
    tests).  :class:`CursorAhead` propagates: it means the database was rebuilt
    and only the caller knows whether to replay.  Each round is a
    :func:`poll_once`, so a busy database simply produces no events that round.
    """
    for events, _cursor, _advanced in _rounds(
        db, cursor, interval=interval, stop=stop, include_edits=include_edits, sleep=sleep
    ):
        yield from events


def run_watch(
    db: ChatDB,
    on_event: Callable[[Event], None],
    *,
    on_cursor: Callable[[Cursor], None] | None = None,
    cursor: Cursor | None = None,
    interval: float = 2.0,
    stop: threading.Event | None = None,
    include_edits: bool = True,
    sleep: Callable[[float], object] = time.sleep,
) -> None:
    """Callback form of :func:`watch`.

    ``on_event`` receives every event in order; ``on_cursor`` (when given)
    receives the new :class:`Cursor` after each round that advanced it - the
    hook for persisting progress with :meth:`Cursor.to_json`.  The remaining
    keywords are :func:`watch`'s.  Returns when ``stop`` is set.
    """
    for events, cur, advanced in _rounds(
        db, cursor, interval=interval, stop=stop, include_edits=include_edits, sleep=sleep
    ):
        for ev in events:
            on_event(ev)
        if advanced and on_cursor is not None:
            on_cursor(cur)
