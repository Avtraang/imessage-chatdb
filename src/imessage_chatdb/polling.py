"""Polling for new, edited and unsent (retracted) messages (DESIGN.md section 4.6).

The unit of progress is a frozen :class:`Cursor`: the last ROWID delivered plus
two raw Apple high-water marks, ``date_edited`` and ``date_retracted`` (``int``
values that are never converted).  :func:`poll_once` is the primitive - one
round, one connection, pure data in and out; :func:`watch` and
:func:`run_watch` loop over it with an injectable ``sleep`` and an optional
stop :class:`threading.Event`.

Every :class:`Event` carries the :class:`Cursor` to persist once *that* event
has been handled (``event.cursor``), so a consumer of the :func:`watch`
generator can checkpoint after each event and, after a crash, resume from the
last checkpoint with at-least-once delivery - nothing between the checkpoint
and the crash is lost, and the event that was being handled is delivered
again.  Edited rows that share one raw ``date_edited`` (a tie) are treated as
a group: the mark advances past the value only on the group's last event, so
resuming from an earlier member's cursor re-delivers the whole group rather
than skipping its remaining members (the resume query is strict,
``date_edited > mark``).  Retracted rows tied on ``date_retracted`` follow the
same rule with ``retract_mark``.

The rules of a round are those of the relay's polling loop:
new rows strictly after the cursor, ascending by ROWID; then rows whose
``date_edited`` advanced past the mark, ascending by ``date_edited``; then
(the library's own, 0.2) rows whose ``date_retracted`` advanced past the
retract mark, ascending by ``date_retracted``; the ROWID cursor advances only
to the last row delivered (at-least-once), each mark only forward.  Two more
things are the library's own: a database whose ``MAX(ROWID)`` fell below the
cursor raises :class:`CursorAhead` (the relay's generic ``except`` would spin
on it), and a locked database yields ``([], cursor)`` so a caller can simply
try again next round.

An unsend ("retraction") does not delete the row: Messages.app keeps it, sets
``date_retracted`` and is reported to clear its text.  A ``"retracted"`` event
therefore carries the row *as it is now* (``message.text`` typically
``None``); the caller's job is to remove or mark the message it delivered
earlier, keyed on ``rowid`` or ``guid``, not to read content from the event.

This module is ``imessage_chatdb.polling``; its five names (``Cursor``,
``Event``, ``poll_once``, ``watch``, ``run_watch``) are re-exported by the
package, so ``from imessage_chatdb import watch`` is the generator function.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from .errors import ChatDBBusy, CursorAhead
from .messages import (
    max_rowid,
    messages_after,
    messages_edited_after,
    messages_retracted_after,
)
from .models import Message

if TYPE_CHECKING:
    from .db import ChatDB

__all__ = ["Cursor", "Event", "poll_once", "watch", "run_watch"]

EventKind = Literal["new", "edited", "retracted"]


@dataclass(frozen=True, slots=True)
class Cursor:
    """Where a watcher is: the last ROWID delivered and the raw edit and retract marks.

    ``edit_mark`` is ``message.date_edited`` as stored (Apple-epoch
    nanoseconds), never converted, so persisting and comparing it is exact;
    ``retract_mark`` is the same for ``message.date_retracted`` (0.2; a state
    file written by 0.1 has no such key and reads as ``0``, which replays
    every unsend once).  ``Cursor()`` (all zero) replays the whole database;
    use ``ChatDB.initial_cursor()`` to start at "now".
    """

    rowid: int = 0
    edit_mark: int = 0
    retract_mark: int = 0

    def to_json(self) -> dict[str, int]:
        """A JSON-ready dict: ``{"rowid": ..., "edit_mark": ..., "retract_mark": ...}``."""
        return {
            "rowid": self.rowid,
            "edit_mark": self.edit_mark,
            "retract_mark": self.retract_mark,
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> Cursor:
        """Inverse of :meth:`to_json`; missing or ``None`` keys read as ``0``
        (so a ``{"rowid", "edit_mark"}`` dict from 0.1 still loads)."""
        return cls(
            rowid=int(d.get("rowid") or 0),
            edit_mark=int(d.get("edit_mark") or 0),
            retract_mark=int(d.get("retract_mark") or 0),
        )


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that happened: a ``"new"`` message, an ``"edited"`` one or a
    ``"retracted"`` (unsent) one.

    ``cursor`` is the :class:`Cursor` to persist once this event has been
    handled - resuming from it re-delivers nothing before this event and
    everything after it (at-least-once: the event itself is delivered again
    only if the crash happened between handling it and persisting).  Within a
    round, a ``"new"`` event's cursor is ``Cursor(rowid=<its ROWID>,
    edit_mark=<the round's starting edit mark>, retract_mark=<the round's
    starting retract mark>)``; an ``"edited"`` event's cursor is
    ``Cursor(rowid=<the round's final ROWID>, edit_mark=<the largest raw
    date_edited of the events so far, excluding this event's own value while
    a later event in the round shares it>, retract_mark=<the round's starting
    retract mark>)``; a ``"retracted"`` event's cursor is ``Cursor(rowid=<the
    round's final ROWID>, edit_mark=<the round's final edit mark>,
    retract_mark=<the largest raw date_retracted so far, with the same
    exclusion>)``.  That exclusion is the tie rule: rows with the same raw
    mark value form a group, only the group's last event advances the mark
    past that value, and resuming from any earlier member's cursor
    re-delivers the whole group (the already handled members again,
    at-least-once) instead of losing the rest of it.  The last event's cursor
    is the cursor :func:`poll_once` returns for the round.

    A ``"retracted"`` event's ``message`` is the row as it is now - an unsent
    message keeps its row with ``date_retracted`` set and its text typically
    cleared - so handle it by ``rowid``/``guid``: remove or mark the message
    you delivered before, do not expect its content.
    """

    kind: EventKind
    message: Message
    cursor: Cursor


def _tail_marks(rows: list[Message], values: list[int], start: int) -> list[int]:
    """The per-event mark for ``rows`` whose raw mark values are ``values``.

    Rows are ordered by their mark, so ties are adjacent.  The resume query is
    strict (``> mark``): advancing the mark to a value on an event that still
    has tied siblings after it would make a checkpoint taken there skip those
    siblings.  So the mark passes a value only on the last event carrying it;
    earlier members keep the previous mark and a resume from them replays the
    whole tie (at-least-once).  The final entry equals ``max(start, *values)``
    - the round's mark - because the last row is always the last of its value.
    """
    out: list[int] = []
    running = start
    for i, value in enumerate(values):
        last_of_value = i + 1 == len(rows) or values[i + 1] != value
        if last_of_value:
            running = max(running, value)
        out.append(running)
    return out


def poll_once(
    db: ChatDB,
    cursor: Cursor,
    *,
    include_edits: bool = True,
    include_retractions: bool = False,
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
    - Unsends are fetched only when ``include_retractions`` (off by default:
      a consumer written for 0.1 sees exactly the 0.1 stream and must opt in
      to the third kind) and the schema has ``message.date_retracted``
      (macOS 26+); ``retract_mark`` follows the same rule and does not move
      while the kind is off, so switching it on later delivers the unsends
      missed meanwhile.  The event carries the row as it is now (text
      typically cleared).
    - ``"new"`` events precede ``"edited"`` events, which precede
      ``"retracted"`` events within the round.  A row that is both new and
      edited since the mark appears twice, as in the relay; one that was also
      unsent appears a third time.
    - Each event's ``cursor`` is the one to persist after handling it (see
      :class:`Event`); the last event's cursor equals ``new_cursor``.  Rows
      tied on ``date_edited`` (or ``date_retracted``) advance that mark only on
      the tie's last event, so a checkpoint inside a tie replays the tie,
      never skips it.
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
            retracted: list[Message] = []
            rmark = cursor.retract_mark
            if include_retractions and schema.has("message", "date_retracted"):
                retracted, rmark = messages_retracted_after(conn, schema, cursor.retract_mark)
                if rmark < cursor.retract_mark:  # defensive, as above
                    rmark = cursor.retract_mark
    except ChatDBBusy:
        return [], cursor
    start_edit, start_retract = cursor.edit_mark, cursor.retract_mark
    events: list[Event] = [
        Event("new", m, Cursor(rowid=m.rowid, edit_mark=start_edit, retract_mark=start_retract))
        for m in new
    ]
    # Edited events come before every retracted event, so their cursors keep
    # the round's starting retract mark: a resume from one replays all unsends.
    edit_marks = _tail_marks(edited, [m.date_edited or 0 for m in edited], start_edit)
    events.extend(
        Event("edited", m, Cursor(rowid=rowid, edit_mark=em, retract_mark=start_retract))
        for m, em in zip(edited, edit_marks, strict=True)
    )
    # Retracted events come last: every edit has been handled, so they carry
    # the round's final edit mark and a running retract mark.
    retract_marks = _tail_marks(
        retracted, [m.date_retracted or 0 for m in retracted], start_retract
    )
    events.extend(
        Event("retracted", m, Cursor(rowid=rowid, edit_mark=mark, retract_mark=rm))
        for m, rm in zip(retracted, retract_marks, strict=True)
    )
    # Each running mark ends at its round mark (same rows, same raw values, and
    # the final event is always the last of its value), so the last event's
    # cursor is exactly the round's cursor; test_watch.py / test_retractions.py
    # pin it in every shape.
    return events, Cursor(rowid=rowid, edit_mark=mark, retract_mark=rmark)


def _rounds(
    db: ChatDB,
    cursor: Cursor | None,
    *,
    interval: float,
    stop: threading.Event | None,
    include_edits: bool,
    include_retractions: bool,
    sleep: Callable[[float], object],
) -> Iterator[tuple[list[Event], Cursor, bool]]:
    """Yield ``(events, cursor, advanced)`` per round until ``stop`` is set.

    The stop flag is checked before every round and again after it (before the
    sleep), so a consumer that sets it while handling events never waits a
    full ``interval`` for the generator to notice.
    """
    cur = db.initial_cursor() if cursor is None else cursor
    while stop is None or not stop.is_set():
        events, new_cur = poll_once(
            db, cur, include_edits=include_edits, include_retractions=include_retractions
        )
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
    include_retractions: bool = False,
    sleep: Callable[[float], object] = time.sleep,
) -> Iterator[Event]:
    """Yield :class:`Event` objects forever (or until ``stop`` is set).

    ``cursor=None`` starts at ``db.initial_cursor()`` - "now", with no replay.
    Persist ``event.cursor`` (:meth:`Cursor.to_json`) after handling each
    event and pass it back as ``cursor`` on restart to resume where you
    stopped.  Between rounds the generator calls ``sleep(interval)``
    (injectable for tests).  ``include_edits`` switches ``"edited"`` events
    off; ``include_retractions=True`` switches ``"retracted"`` events on
    (off by default, see :func:`poll_once`).  :class:`CursorAhead`
    propagates: it means the database was rebuilt and only the caller knows
    whether to replay.  Each round is a :func:`poll_once`, so a busy database
    simply produces no events that round.
    """
    for events, _cursor, _advanced in _rounds(
        db,
        cursor,
        interval=interval,
        stop=stop,
        include_edits=include_edits,
        include_retractions=include_retractions,
        sleep=sleep,
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
    include_retractions: bool = False,
    sleep: Callable[[float], object] = time.sleep,
) -> None:
    """Callback form of :func:`watch`.

    ``on_event`` receives every event in order (each carrying its own
    ``cursor``); ``on_cursor`` (when given) receives the new :class:`Cursor`
    after each round that advanced it - the per-round hook for persisting
    progress with :meth:`Cursor.to_json`.  The remaining keywords are
    :func:`watch`'s.  Returns when ``stop`` is set.
    """
    for events, cur, advanced in _rounds(
        db,
        cursor,
        interval=interval,
        stop=stop,
        include_edits=include_edits,
        include_retractions=include_retractions,
        sleep=sleep,
    ):
        for ev in events:
            on_event(ev)
        if advanced and on_cursor is not None:
            on_cursor(cur)
