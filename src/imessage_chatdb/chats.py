"""Chats, participants, activity, previews and chat matching (DESIGN.md 4.4, 6.4).

Every function takes an open ``sqlite3.Connection`` and returns plain facts
from the ``chat`` side of the schema.  Statements whose row order or row set is
JSON-visible in the relay are executed verbatim from :mod:`imessage_chatdb.sql`
(``THREADS``, ``PARTICIPANTS``, ``PARTICIPANTS_MAP``, ``CHAT_SERVICES``,
``UNREAD``, ``LAST_ROWID``, ``LITE_MESSAGES``, ``FIND_1TO1``,
``FIND_GROUP_JOINS``, ``FIND_GROUP_SENDERS``, ``ONE_TO_ONE_ACTIVITY``).  The
optional ``chat`` columns (``service_name``, ``is_archived``, ``is_filtered``,
``group_id``) are read through a small schema-tolerant SELECT of their own, so
nothing here needs a :class:`~imessage_chatdb.schema.Schema`.

Presentation -- contact names, first-name group titles, preview strings,
"You"/"me" -- is deliberately absent; see the relay for those.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Sequence

from . import sql
from .attachments import attachments_for
from .handles import address_key
from .models import Chat, ChatMatch, ChatSummary, LiteMessage
from .services import service_family
from .typedstream import clean_text, effective_text

__all__ = [
    "chats_by_activity",
    "chat",
    "chat_by_rowid",
    "participants",
    "participants_map",
    "chat_services",
    "last_rowid_for",
    "unread_count",
    "lite_messages",
    "one_to_one_activity",
    "find_chat",
]

#: ``chat.style`` of a group chat (``45`` is one-to-one).
GROUP_STYLE = 43
ONE_TO_ONE_STYLE = 45

#: The relay's guid tie-break for one-to-one matches (a no-op on ``any;`` guids,
#: kept verbatim for fidelity).
_IMESSAGE_GUID_PREFIX = "iMessage"

#: Optional ``chat`` columns, in the order the tolerant SELECT emits them.
_CHAT_OPTIONAL: tuple[str, ...] = ("service_name", "is_archived", "is_filtered", "group_id")

#: ``UNREAD`` without its incoming-only predicate, for ``incoming_only=False``.
_UNREAD_ANY = sql.UNREAD.replace(" AND m.is_from_me = 0", "")
assert _UNREAD_ANY != sql.UNREAD


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _rows(
    conn: sqlite3.Connection, statement: str, params: tuple[object, ...] = ()
) -> list[sqlite3.Row]:
    """Execute and ``fetchall()`` with name-addressable rows regardless of ``conn.row_factory``."""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    try:
        return list(cur.execute(statement, params).fetchall())
    finally:
        cur.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    """Lower-cased column names of ``table`` (``PRAGMA table_info``)."""
    return frozenset(str(r[1]).lower() for r in conn.execute(f"PRAGMA table_info({table})"))


def _opt_bool(v: object) -> bool | None:
    return None if v is None else bool(v)


def _chat_select(conn: sqlite3.Connection) -> str:
    """Schema-tolerant ``chat`` SELECT; missing optional columns render as ``NULL AS``."""
    present = _table_columns(conn, "chat")
    optional = ", ".join(
        f"c.{col} AS {col}" if col in present else f"NULL AS {col}" for col in _CHAT_OPTIONAL
    )
    return (
        "SELECT c.ROWID AS rowid, c.guid AS guid, c.style AS style, "
        "c.chat_identifier AS chat_identifier, c.display_name AS display_name, "
        f"{optional} FROM chat c"
    )


def _row_to_chat(r: sqlite3.Row) -> Chat:
    return Chat(
        rowid=int(r["rowid"]),
        guid=str(r["guid"]),
        style=None if r["style"] is None else int(r["style"]),
        chat_identifier=r["chat_identifier"],
        display_name=r["display_name"],
        service_name=r["service_name"],
        is_archived=_opt_bool(r["is_archived"]),
        is_filtered=_opt_bool(r["is_filtered"]),
        group_id=r["group_id"],
    )


def _chats_by_rowid(conn: sqlite3.Connection, rowids: Sequence[int]) -> dict[int, Chat]:
    if not rowids:
        return {}
    statement = _chat_select(conn) + " WHERE c.ROWID IN (" + ",".join("?" * len(rowids)) + ")"
    return {c.rowid: c for c in (_row_to_chat(r) for r in _rows(conn, statement, tuple(rowids)))}


# ---------------------------------------------------------------------------
# chats
# ---------------------------------------------------------------------------


def chats_by_activity(conn: sqlite3.Connection, *, limit: int = 200) -> list[ChatSummary]:
    """Chats with at least one message, newest activity first (the verbatim ``THREADS`` statement).

    ``THREADS`` verbatim: ``GROUP BY c.ROWID ORDER BY last_rowid DESC LIMIT ?``;
    chats without a ``chat_message_join`` row are excluded by the inner joins.
    ``last_date`` is ``MAX(m.date)`` and ``last_rowid`` ``MAX(m.ROWID)``.  The
    optional ``chat`` columns are filled by one extra ``IN`` query (``None`` when
    the schema lacks them).
    """
    rows = _rows(conn, sql.THREADS, (limit,))
    extra = _chats_by_rowid(conn, [int(r["chat_rowid"]) for r in rows])
    out: list[ChatSummary] = []
    for r in rows:
        rid = int(r["chat_rowid"])
        c = extra.get(rid)
        out.append(
            ChatSummary(
                rowid=rid,
                guid=str(r["chat_guid"]),
                style=None if r["chat_style"] is None else int(r["chat_style"]),
                chat_identifier=r["chat_identifier"],
                display_name=r["chat_name"],
                service_name=None if c is None else c.service_name,
                is_archived=None if c is None else c.is_archived,
                is_filtered=None if c is None else c.is_filtered,
                group_id=None if c is None else c.group_id,
                last_date=None if r["last_date"] is None else int(r["last_date"]),
                last_rowid=int(r["last_rowid"]),
            )
        )
    return out


def chat(conn: sqlite3.Connection, guid: str) -> Chat | None:
    """The ``chat`` row with ``guid``, or ``None``."""
    rows = _rows(conn, _chat_select(conn) + " WHERE c.guid = ?", (guid,))
    return _row_to_chat(rows[0]) if rows else None


def chat_by_rowid(conn: sqlite3.Connection, rowid: int) -> Chat | None:
    """The ``chat`` row with ``ROWID``, or ``None``."""
    rows = _rows(conn, _chat_select(conn) + " WHERE c.ROWID = ?", (rowid,))
    return _row_to_chat(rows[0]) if rows else None


def participants(conn: sqlite3.Connection, chat_rowid: int) -> list[str]:
    """Raw ``handle.id`` of each ``chat_handle_join`` row.

    The verbatim ``PARTICIPANTS`` statement (no ORDER BY).
    """
    return [r["id"] for r in _rows(conn, sql.PARTICIPANTS, (chat_rowid,))]


def participants_map(conn: sqlite3.Connection) -> dict[int, list[str]]:
    """chat ROWID -> raw handle ids for every ``chat_handle_join`` row.

    The verbatim ``PARTICIPANTS_MAP`` statement (no ORDER BY).
    """
    out: dict[int, list[str]] = {}
    for r in _rows(conn, sql.PARTICIPANTS_MAP):
        out.setdefault(int(r["rid"]), []).append(r["hid"])
    return out


def chat_services(conn: sqlite3.Connection, chat_rowids: Iterable[int]) -> dict[int, str | None]:
    """chat ROWID -> ``"iMessage"`` | ``"RCS"`` | ``"SMS"`` | ``None`` (section 6.4).

    ``CHAT_SERVICES`` verbatim: the newest **outgoing** message with a non-empty
    service, else the newest message with one, else ``chat.service_name``, each
    through :func:`service_family` (which folds ``iMessageLite`` into iMessage).
    Raises on SQL failure -- the relay wrapper is what turns that into ``{}``.
    Empty input -> ``{}`` without a query.
    """
    ids = [int(i) for i in chat_rowids if i is not None]
    if not ids:
        return {}
    rows = _rows(conn, sql.expand_in(sql.CHAT_SERVICES, len(ids)), tuple(ids))
    return {
        int(r["rid"]): (
            service_family(r["out_svc"])
            or service_family(r["any_svc"])
            or service_family(r["chat_svc"])
        )
        for r in rows
    }


def last_rowid_for(conn: sqlite3.Connection, chat_rowid: int) -> int:
    """``MAX(m.ROWID)`` of the chat's messages, ``0`` when it has none."""
    rows = _rows(conn, sql.LAST_ROWID, (chat_rowid,))
    return int(rows[0]["m"] or 0) if rows else 0


def unread_count(
    conn: sqlite3.Connection, chat_rowid: int, after_rowid: int, *, incoming_only: bool = True
) -> int:
    """Messages of the chat with ``ROWID > after_rowid`` (the relay's UNREAD SQL verbatim).

    ``incoming_only=True`` keeps the relay's ``m.is_from_me = 0`` predicate;
    ``False`` counts outgoing rows too.
    """
    statement = sql.UNREAD if incoming_only else _UNREAD_ANY
    rows = _rows(conn, statement, (chat_rowid, after_rowid))
    return int(rows[0]["c"]) if rows else 0


# ---------------------------------------------------------------------------
# previews
# ---------------------------------------------------------------------------


def _emoji_for(conn: sqlite3.Connection, rowids: Sequence[int]) -> dict[int, str | None]:
    """``associated_message_emoji`` per ROWID; ``{}`` when the column is absent."""
    if not rowids or "associated_message_emoji" not in _table_columns(conn, "message"):
        return {}
    statement = (
        "SELECT ROWID AS rowid, associated_message_emoji AS emoji FROM message "
        "WHERE ROWID IN (" + ",".join("?" * len(rowids)) + ")"
    )
    return {int(r["rowid"]): r["emoji"] for r in _rows(conn, statement, tuple(rowids))}


def lite_messages(conn: sqlite3.Connection, rowids: Iterable[int]) -> dict[int, LiteMessage]:
    """ROWID -> preview facts for the given messages (the verbatim ``LITE_MESSAGES`` statement).

    ``text`` is ``clean_text(effective_text(text, attributedBody))`` (``''`` when
    none); ``associated_type`` is ``assoc_type or 0``; ``attachments`` are looked
    up only for ``has_attachments`` rows (default plugin-payload exclusion) and
    are ``()`` otherwise.  ``associated_emoji`` is read by one extra query when
    the schema has the column.  Unknown ROWIDs are simply absent; empty input
    -> ``{}`` without a query.
    """
    ids = [int(i) for i in rowids]
    if not ids:
        return {}
    rows = _rows(conn, sql.expand_in(sql.LITE_MESSAGES, len(ids)), tuple(ids))
    with_att = [int(r["rowid"]) for r in rows if r["has_attachments"]]
    amap = attachments_for(conn, with_att) if with_att else {}
    emoji = _emoji_for(conn, [int(r["rowid"]) for r in rows])
    out: dict[int, LiteMessage] = {}
    for r in rows:
        rid = int(r["rowid"])
        out[rid] = LiteMessage(
            rowid=rid,
            text=clean_text(effective_text(r["text"], r["attributed_body"])),
            is_from_me=bool(r["is_from_me"]),
            associated_type=int(r["assoc_type"] or 0),
            associated_emoji=emoji.get(rid),
            has_attachments=bool(r["has_attachments"]),
            sender_handle=r["sender"],
            attachments=tuple(amap.get(rid, ())) if r["has_attachments"] else (),
        )
    return out


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------


def one_to_one_activity(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """``(chat_identifier, last_rowid)`` for every one-to-one chat with messages.

    The verbatim ``ONE_TO_ONE_ACTIVITY`` statement (``style = 45 GROUP BY c.ROWID``).
    Rows whose ``chat_identifier`` is NULL are skipped (the relay could never map
    them to a contact either).
    """
    return [
        (str(r["ci"]), int(r["last"] or 0))
        for r in _rows(conn, sql.ONE_TO_ONE_ACTIVITY)
        if r["ci"] is not None
    ]


def _match(conn: sqlite3.Connection, rowid: int, last_rowid: int) -> ChatMatch | None:
    """``ChatMatch`` for the winning chat; ``None`` if it vanished between the two reads."""
    c = chat_by_rowid(conn, rowid)
    if c is None:
        return None
    return ChatMatch(
        chat_rowid=c.rowid,
        chat_guid=c.guid,
        chat_identifier=c.chat_identifier,
        display_name=c.display_name,
        is_group=c.is_group,
        last_rowid=last_rowid,
    )


def find_chat(
    conn: sqlite3.Connection,
    addresses: Sequence[str],
    *,
    key: Callable[[str], str | None] = address_key,
    exclude: Iterable[str] = (),
) -> ChatMatch | None:
    """Match an address set to the most recently active existing chat.

    ``self = {key(a) for a in exclude}``; ``want = {key(a) for a in addresses} - self``;
    an empty ``want`` -> ``None``.

    - **One-to-one branch, taken iff ``len(addresses) == 1``** (the relay's rule;
      *not* the size of ``want``, so two addresses that ``key`` collapses to one
      person still take the group branch): ``style = 45`` chats whose
      ``key(chat_identifier)`` is in ``want``; the best is
      ``max`` by ``(last_rowid_for, guid.startswith("iMessage"))``.
    - **Group branch**: ``style = 43`` chats; participants = ``chat_handle_join``
      rows (LEFT JOIN) **union** distinct incoming message senders, each passed
      through ``key`` with ``self`` removed; a chat matches on exact set
      equality with ``want``; the newest ``last_rowid_for`` wins.

    ``key`` may return ``None`` for an address it cannot normalise; such values
    simply never compare equal to a real key.  The default ``key`` is
    :func:`~imessage_chatdb.handles.address_key`.
    """
    self_people = {key(a) for a in exclude}
    want = {key(a) for a in addresses} - self_people
    if not want:
        return None

    if len(addresses) == 1:
        rows = _rows(conn, sql.FIND_1TO1)
        cands = [r for r in rows if r["ci"] is not None and key(r["ci"]) in want]
        if not cands:
            return None
        best = max(
            cands,
            key=lambda r: (
                last_rowid_for(conn, int(r["rid"])),
                str(r["guid"]).startswith(_IMESSAGE_GUID_PREFIX),
            ),
        )
        rid = int(best["rid"])
        return _match(conn, rid, last_rowid_for(conn, rid))

    groups: dict[int, set[str | None]] = {}
    for r in _rows(conn, sql.FIND_GROUP_JOINS):
        people = groups.setdefault(int(r["rid"]), set())
        if r["hid"]:
            pk = key(r["hid"])
            if pk not in self_people:
                people.add(pk)
    for r in _rows(conn, sql.FIND_GROUP_SENDERS):
        rid = int(r["rid"])
        if rid in groups and r["hid"]:
            pk = key(r["hid"])
            if pk not in self_people:
                groups[rid].add(pk)

    group_cands = [grid for grid, people in groups.items() if people == want]
    if not group_cands:
        return None
    winner = max(group_cands, key=lambda grid: last_rowid_for(conn, grid))
    return _match(conn, winner, last_rowid_for(conn, winner))
