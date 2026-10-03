"""Message queries (DESIGN.md sections 4.4, 6.1, 8.4 messages/edits).

Every function takes an open read-only ``sqlite3.Connection`` whose
``row_factory`` is ``sqlite3.Row`` (what :func:`imessage_chatdb.open_connection`
returns) and, where the message SELECT is used, a :class:`Schema` so the
statement is built for the columns that exist.  Every query ends in
``fetchall()``; nothing here holds a live cursor.

The SELECT is :func:`imessage_chatdb.schema.build_message_select` plus the
relay's verbatim suffixes from :mod:`imessage_chatdb.sql`, so the row set and
row order are the relay's.  :func:`row_to_message` is the relay's
row conversion without its strings: ``effective_text`` for ``text``, ``bool()``
on ``is_from_me`` / ``has_attachments``, the link preview parsed only when
``balloon_bundle_id`` is ``LINK_BALLOON``, and every optional column that the
schema lacks (or the row does not carry) rendered as ``None``.
:func:`enrich` is the relay's enrichment step: one ``IN`` query for attachments
(only for ``has_attachments`` rows) and one for reply targets.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Iterable
from typing import Any

from .attachments import attachments_for
from .link_preview import LINK_BALLOON, parse_link_preview
from .models import Message, ReplyTarget
from .schema import Schema, build_message_select
from .sql import (
    AFTER_ROWID_SUFFIX,
    EDITED_AFTER_SUFFIX,
    MAX_DATE_EDITED,
    MAX_ROWID,
    REPLY_TARGETS,
    THREAD_BEFORE_SUFFIX,
    THREAD_ORDER_SUFFIX,
    THREAD_WHERE_SUFFIX,
    expand_in,
)
from .typedstream import clean_text, effective_text

__all__ = [
    "row_to_message",
    "messages_after",
    "messages_edited_after",
    "max_rowid",
    "max_date_edited",
    "thread_messages",
    "recent_messages",
    "message",
    "messages_by_guid",
    "reply_targets",
    "enrich",
]

# Suffixes that are not JSON-visible in the relay (point lookups).
_BY_ROWID_SUFFIX = " WHERE m.ROWID = ?"
_BY_GUID_SUFFIX = " WHERE m.guid IN ({ph})"
_LIMIT_SUFFIX = " LIMIT ?"


# ---------------------------------------------------------------------------
# row -> model
# ---------------------------------------------------------------------------


def _opt(r: sqlite3.Row, keys: set[str], name: str) -> Any:
    """``r[name]`` when the row carries ``name``, else ``None``."""
    return r[name] if name in keys else None


def _opt_int(r: sqlite3.Row, keys: set[str], name: str) -> int | None:
    v = _opt(r, keys, name)
    return None if v is None else int(v)


def _opt_str(r: sqlite3.Row, keys: set[str], name: str) -> str | None:
    v = _opt(r, keys, name)
    return None if v is None else str(v)


def _opt_bool(r: sqlite3.Row, keys: set[str], name: str) -> bool | None:
    v = _opt(r, keys, name)
    return None if v is None else bool(v)


def row_to_message(r: sqlite3.Row) -> Message:
    """Build a ``Message`` from one row of :func:`build_message_select`.

    - ``text`` is ``effective_text(m.text, m.attributedBody)``; ``text_column``
      keeps the raw column.
    - ``is_from_me`` and ``has_attachments`` are ``bool()``-ed.
    - ``link`` is parsed only when ``balloon_bundle_id == LINK_BALLOON``
      (so it is always ``None`` when the schema lacks the column).
    - Optional columns the schema lacks arrive as ``NULL AS alias`` and become
      ``None``; aliases the row does not carry at all are ``None`` too.
    - ``attachments`` / ``reply_to`` are left empty; :func:`enrich` fills them.

    With ``include_orphans`` rows the ``chat_*`` values are ``None``.
    """
    keys = set(r.keys())
    balloon = _opt_str(r, keys, "balloon_bundle_id")
    link = None
    if balloon == LINK_BALLOON:
        link = parse_link_preview(_opt(r, keys, "payload_data"))
    text_column = r["text"]
    body = r["attributed_body"]
    return Message(
        rowid=int(r["rowid"]),
        guid=str(r["guid"]),
        text=effective_text(text_column, body),
        text_column=text_column,
        date=int(r["date"] or 0),
        date_read=_opt_int(r, keys, "date_read"),
        date_delivered=_opt_int(r, keys, "date_delivered"),
        date_edited=_opt_int(r, keys, "date_edited"),
        date_retracted=_opt_int(r, keys, "date_retracted"),
        is_from_me=bool(r["is_from_me"]),
        handle_rowid=_opt_int(r, keys, "handle_rowid"),
        sender_handle=_opt_str(r, keys, "sender"),
        # int / str for joined rows, None for include_orphans rows.
        chat_rowid=_opt_int(r, keys, "chat_rowid"),
        chat_guid=_opt_str(r, keys, "chat_guid"),
        chat_identifier=_opt_str(r, keys, "chat_identifier"),
        chat_display_name=_opt_str(r, keys, "chat_name"),
        chat_style=_opt_int(r, keys, "chat_style"),
        service_raw=_opt_str(r, keys, "service"),
        has_attachments=bool(r["has_attachments"]),
        associated_guid=_opt_str(r, keys, "assoc_guid"),
        associated_type=_opt_int(r, keys, "assoc_type"),
        associated_emoji=_opt_str(r, keys, "assoc_emoji"),
        reply_to_guid=_opt_str(r, keys, "reply_to_guid"),
        reply_to_part=_opt_str(r, keys, "reply_to_part"),
        balloon_bundle_id=balloon,
        link=link,
        item_type=_opt_int(r, keys, "item_type"),
        group_action_type=_opt_int(r, keys, "group_action_type"),
        group_title=_opt_str(r, keys, "group_title"),
        filter_action=_opt_int(r, keys, "filter_action"),
        is_spam=_opt_bool(r, keys, "is_spam"),
        expressive_send_style_id=_opt_str(r, keys, "expressive_send_style_id"),
    )


# ---------------------------------------------------------------------------
# enrichment
# ---------------------------------------------------------------------------


def reply_targets(conn: sqlite3.Connection, guids: Iterable[str]) -> dict[str, ReplyTarget]:
    """The messages the given GUIDs name, as reply targets, keyed by GUID.

    One ``IN`` query (the verbatim ``REPLY_TARGETS`` statement); none when
    ``guids`` is empty.  ``text`` is ``clean_text(effective_text(...))``,
    untruncated, ``''`` for an attachment-only target.  GUIDs with no row are
    absent from the result.
    """
    wanted = list(dict.fromkeys(g for g in guids if g))
    if not wanted:
        return {}
    rows = conn.execute(expand_in(REPLY_TARGETS, len(wanted)), tuple(wanted)).fetchall()
    out: dict[str, ReplyTarget] = {}
    for r in rows:
        guid = str(r["guid"])
        out[guid] = ReplyTarget(
            guid=guid,
            text=clean_text(effective_text(r["text"], r["ab"])),
            is_from_me=bool(r["mine"]),
            sender_handle=None if r["sender"] is None else str(r["sender"]),
        )
    return out


def enrich(conn: sqlite3.Connection, msgs: list[Message]) -> list[Message]:
    """Fill ``attachments`` and ``reply_to`` on a batch; returns new ``Message`` objects.

    The relay's enrichment step: attachments are fetched with one ``IN`` query and
    only for ``has_attachments`` rows; reply targets with one ``IN`` query over
    the distinct ``reply_to_guid`` values.  No query is issued when there is
    nothing to fetch.  Every returned message has ``enriched=True``; the input
    objects are not mutated (frozen).
    """
    if not msgs:
        return []
    with_att = [m.rowid for m in msgs if m.has_attachments]
    amap = attachments_for(conn, with_att)
    targets = reply_targets(conn, (m.reply_to_guid for m in msgs if m.reply_to_guid))
    out: list[Message] = []
    for m in msgs:
        reply = targets.get(m.reply_to_guid) if m.reply_to_guid else None
        out.append(
            dataclasses.replace(
                m,
                attachments=tuple(amap.get(m.rowid, ())),
                reply_to=reply,
                enriched=True,
            )
        )
    return out


def _finish(conn: sqlite3.Connection, rows: list[sqlite3.Row], do_enrich: bool) -> list[Message]:
    msgs = [row_to_message(r) for r in rows]
    return enrich(conn, msgs) if do_enrich else msgs


# ---------------------------------------------------------------------------
# cursors
# ---------------------------------------------------------------------------


def max_rowid(conn: sqlite3.Connection) -> int:
    """``MAX(ROWID)`` of ``message``, or ``0`` on an empty table."""
    rows = conn.execute(MAX_ROWID).fetchall()
    value = rows[0]["m"] if rows else None
    return int(value) if value else 0


def max_date_edited(conn: sqlite3.Connection, schema: Schema) -> int:
    """``MAX(date_edited)`` as a raw Apple int, or ``0`` (also when the column is absent)."""
    if not schema.has("message", "date_edited"):
        return 0
    rows = conn.execute(MAX_DATE_EDITED).fetchall()
    value = rows[0]["m"] if rows else None
    return int(value) if value else 0


# ---------------------------------------------------------------------------
# fetches
# ---------------------------------------------------------------------------


def messages_after(
    conn: sqlite3.Connection,
    schema: Schema,
    rowid: int,
    *,
    limit: int | None = None,
    enrich: bool = True,
    include_orphans: bool = False,
) -> list[Message]:
    """Messages with ``ROWID > rowid``, ascending by ROWID (the relay's new-message fetch).

    ``limit`` caps the batch (``None`` = all); ``include_orphans`` switches the
    chat joins to ``LEFT JOIN`` so rows without a ``chat_message_join`` row are
    returned with ``None`` chat fields.
    """
    q = build_message_select(schema, include_orphans=include_orphans) + AFTER_ROWID_SUFFIX
    args: list[object] = [rowid]
    if limit is not None:
        q += _LIMIT_SUFFIX
        args.append(limit)
    rows = conn.execute(q, tuple(args)).fetchall()
    return _finish(conn, rows, enrich)


def messages_edited_after(
    conn: sqlite3.Connection,
    schema: Schema,
    mark: int,
    *,
    enrich: bool = True,
) -> tuple[list[Message], int]:
    """Messages whose raw ``date_edited`` is past ``mark``, ascending by ``date_edited``.

    Returns ``(messages, new_mark)`` where ``new_mark`` is the largest raw
    ``date_edited`` seen and never less than ``mark`` (the relay's
    edited-message fetch).  ``([], mark)`` when the schema has no ``date_edited``.
    """
    if not schema.has("message", "date_edited"):
        return [], mark
    q = build_message_select(schema) + EDITED_AFTER_SUFFIX
    rows = conn.execute(q, (mark,)).fetchall()
    new_mark = mark
    for r in rows:
        de = r["date_edited"]
        if de and int(de) > new_mark:
            new_mark = int(de)
    return _finish(conn, rows, enrich), new_mark


def thread_messages(
    conn: sqlite3.Connection,
    schema: Schema,
    chat_guid: str,
    *,
    limit: int = 50,
    before_rowid: int | None = None,
    enrich: bool = True,
) -> list[Message]:
    """The newest ``limit`` messages of a chat, returned oldest-first.

    The relay's thread page: ``WHERE c.guid = ? [AND m.ROWID < ?]
    ORDER BY m.ROWID DESC LIMIT ?``, then reversed.  ``before_rowid`` pages
    backwards; as in the relay, a falsy value (``None`` or ``0``) means no bound.
    """
    rows = _thread_rows(conn, schema, chat_guid, limit, before_rowid)
    rows.reverse()
    return _finish(conn, rows, enrich)


def recent_messages(
    conn: sqlite3.Connection,
    schema: Schema,
    chat_guid: str,
    *,
    limit: int = 1000,
    enrich: bool = False,
) -> list[Message]:
    """The newest ``limit`` messages of a chat, newest-first (not reversed).

    The order the relay's media endpoint scans for links; ``enrich`` is off
    by default because that scan only needs text.
    """
    rows = _thread_rows(conn, schema, chat_guid, limit, None)
    return _finish(conn, rows, enrich)


def _thread_rows(
    conn: sqlite3.Connection,
    schema: Schema,
    chat_guid: str,
    limit: int,
    before_rowid: int | None,
) -> list[sqlite3.Row]:
    q = build_message_select(schema) + THREAD_WHERE_SUFFIX
    args: list[object] = [chat_guid]
    if before_rowid:
        q += THREAD_BEFORE_SUFFIX
        args.append(before_rowid)
    q += THREAD_ORDER_SUFFIX
    args.append(limit)
    return list(conn.execute(q, tuple(args)).fetchall())


def message(
    conn: sqlite3.Connection,
    schema: Schema,
    rowid: int,
    *,
    enrich: bool = True,
) -> Message | None:
    """One message by ROWID, or ``None`` (orphans excluded, like every other fetch)."""
    q = build_message_select(schema) + _BY_ROWID_SUFFIX
    rows = conn.execute(q, (rowid,)).fetchall()
    if not rows:
        return None
    return _finish(conn, rows[:1], enrich)[0]


def messages_by_guid(
    conn: sqlite3.Connection,
    schema: Schema,
    guids: Iterable[str],
    *,
    enrich: bool = True,
) -> dict[str, Message]:
    """Messages by GUID, keyed by GUID; one ``IN`` query, none for empty input."""
    wanted = list(dict.fromkeys(g for g in guids if g))
    if not wanted:
        return {}
    q = build_message_select(schema) + expand_in(_BY_GUID_SUFFIX, len(wanted))
    rows = conn.execute(q, tuple(wanted)).fetchall()
    return {m.guid: m for m in _finish(conn, rows, enrich)}
