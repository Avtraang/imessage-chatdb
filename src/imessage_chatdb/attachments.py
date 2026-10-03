"""Attachment queries (DESIGN.md sections 4.4, 6.1, 8.4 attachments).

Every function takes an open read-only ``sqlite3.Connection`` whose
``row_factory`` is ``sqlite3.Row`` (what :func:`imessage_chatdb.open_connection`
returns) and ends in ``fetchall()``.

The default exclusion is the ``transfer_name`` suffix
``.pluginPayloadAttachment`` (rich-link payload blobs that Messages renders as
cards), **never** ``hide_attachment``: on a live database ``hide_attachment=1``
also covers a handful of ordinary images that the relay surfaces today
(section 2).  ``include_plugin_payloads=True`` turns the filter off.

The two JSON-visible statements, ``ATTACHMENTS_FOR`` (no ``ORDER BY``: SQLite
scan order) and ``CHAT_ATTACHMENTS`` (``ORDER BY m.date DESC``, ties in scan
order), are executed as the verbatim ``sql.py`` constants with **only** extra
result columns appended to the relay's select list (:func:`_widen`): the
relay's columns come first under the relay's aliases, then ``a.ROWID AS
att_rowid, a.*`` so the ``Attachment`` model can carry ``rowid``, ``filename``
and the optional columns.  Every clause from ``FROM`` onward is byte-equal to
the relay's, and the extra columns are all read from ``a`` by rowid, so the
query plan (and therefore the implicit row order) is the relay's;
``tests/test_attachments.py`` asserts both the text derivation and
``EXPLAIN QUERY PLAN`` equality on every schema profile.  Optional
``attachment`` columns (``uti``, ``total_bytes``, ``is_sticker``,
``hide_attachment``) are read by name when present and are ``None`` otherwise,
so no schema introspection query is needed.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from typing import Any

from .models import PLUGIN_PAYLOAD_SUFFIX, Attachment
from .sql import ATTACHMENTS_FOR, CHAT_ATTACHMENTS, PAYLOAD_FOR, expand_in

__all__ = [
    "is_plugin_payload",
    "attachments_for",
    "attachment_by_guid",
    "chat_attachments",
    "payload_for",
]

#: The columns appended to the relay's select list so ``row_to_attachment``
#: can build the model (``att_rowid`` explicit; ``a.*`` for everything else).
WIDE_EXTRA_COLUMNS = "a.ROWID AS att_rowid, a.*"

_FROM_CLAUSE = re.compile(r"\n\s+FROM ")


def _widen(statement: str, extra: str = WIDE_EXTRA_COLUMNS) -> str:
    """Append ``extra`` to the select list of a verbatim relay statement.

    The statement text from its first ``FROM`` clause onward is returned
    byte-for-byte; only ``, extra`` is inserted at the end of the select list.
    """
    m = _FROM_CLAUSE.search(statement)
    if m is None:
        raise ValueError("statement has no FROM clause on its own line")
    return statement[: m.start()] + ", " + extra + statement[m.start() :]


# The verbatim ATTACHMENTS_FOR + the extra columns.
_ATTACHMENTS_FOR_WIDE = _widen(ATTACHMENTS_FOR)

# The verbatim CHAT_ATTACHMENTS + the owning message's ROWID
# and the extra columns; the ``ORDER BY m.date DESC`` is the relay's.
_CHAT_ATTACHMENTS_WIDE = _widen(CHAT_ATTACHMENTS, "m.ROWID AS mid, " + WIDE_EXTRA_COLUMNS)

# One attachment by guid, with the owning message when it has one (the verbatim
# ATTACHMENT_BY_GUID reads only filename/mime/transfer_name).
_ATTACHMENT_BY_GUID_WIDE = """SELECT maj.message_id AS mid, a.ROWID AS att_rowid, a.*
            FROM attachment a
            LEFT JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
            WHERE a.guid = ?
            ORDER BY maj.message_id LIMIT 1"""


def is_plugin_payload(transfer_name: str | None) -> bool:
    """The relay's filter: ``(transfer_name or "").endswith(".pluginPayloadAttachment")``."""
    return (transfer_name or "").endswith(PLUGIN_PAYLOAD_SUFFIX)


def _opt(r: sqlite3.Row, keys: set[str], name: str) -> Any:
    """``r[name]`` when the row carries ``name``, else ``None`` (missing optional column)."""
    return r[name] if name in keys else None


def _opt_bool(r: sqlite3.Row, keys: set[str], name: str) -> bool | None:
    v = _opt(r, keys, name)
    return None if v is None else bool(v)


def _opt_int(r: sqlite3.Row, keys: set[str], name: str) -> int | None:
    v = _opt(r, keys, name)
    return None if v is None else int(v)


def _opt_str(r: sqlite3.Row, keys: set[str], name: str) -> str | None:
    v = _opt(r, keys, name)
    return None if v is None else str(v)


def row_to_attachment(
    r: sqlite3.Row, *, message_rowid: int, message_date: int | None
) -> Attachment:
    """Build an ``Attachment`` from a ``a.*`` row (plus ``att_rowid``).

    ``is_sticker`` / ``hide_attachment`` become ``bool`` (``None`` when the
    column is absent or NULL); ``total_bytes`` is an ``int`` or ``None``.
    """
    keys = set(r.keys())
    return Attachment(
        message_rowid=message_rowid,
        rowid=int(r["att_rowid"]),
        guid=str(r["guid"]),
        mime_type=_opt_str(r, keys, "mime_type"),
        transfer_name=_opt_str(r, keys, "transfer_name"),
        filename=_opt_str(r, keys, "filename"),
        uti=_opt_str(r, keys, "uti"),
        total_bytes=_opt_int(r, keys, "total_bytes"),
        is_sticker=_opt_bool(r, keys, "is_sticker"),
        hide_attachment=_opt_bool(r, keys, "hide_attachment"),
        message_date=message_date,
    )


def attachments_for(
    conn: sqlite3.Connection,
    rowids: Iterable[int],
    *,
    include_plugin_payloads: bool = False,
) -> dict[int, list[Attachment]]:
    """Attachments of the given message ROWIDs, keyed by message ROWID.

    One ``IN`` query (no ``ORDER BY``, as the relay); no query at all when
    ``rowids`` is empty.  Messages without attachments have no key.  Plugin
    payload rows are skipped unless ``include_plugin_payloads``.
    """
    ids = list(dict.fromkeys(int(x) for x in rowids))
    if not ids:
        return {}
    rows = conn.execute(expand_in(_ATTACHMENTS_FOR_WIDE, len(ids)), tuple(ids)).fetchall()
    out: dict[int, list[Attachment]] = {}
    for r in rows:
        if not include_plugin_payloads and is_plugin_payload(r["transfer_name"]):
            continue
        mid = int(r["mid"])
        out.setdefault(mid, []).append(row_to_attachment(r, message_rowid=mid, message_date=None))
    return out


def attachment_by_guid(conn: sqlite3.Connection, guid: str) -> Attachment | None:
    """The attachment with ``guid``, or ``None``.

    ``message_rowid`` is the lowest joined message ROWID, or ``0`` when the
    attachment has no ``message_attachment_join`` row.  No plugin-payload
    filter: a caller asking by guid wants that row.
    """
    rows = conn.execute(_ATTACHMENT_BY_GUID_WIDE, (guid,)).fetchall()
    if not rows:
        return None
    r = rows[0]
    mid = 0 if r["mid"] is None else int(r["mid"])
    return row_to_attachment(r, message_rowid=mid, message_date=None)


def chat_attachments(
    conn: sqlite3.Connection,
    chat_guid: str,
    *,
    include_plugin_payloads: bool = False,
) -> list[Attachment]:
    """Every attachment in the chat with ``chat_guid``, newest message first.

    The verbatim ``CHAT_ATTACHMENTS`` statement: ``ORDER BY m.date DESC``.  Each
    ``Attachment.message_date`` carries the owning message's raw Apple date.
    """
    rows = conn.execute(_CHAT_ATTACHMENTS_WIDE, (chat_guid,)).fetchall()
    out: list[Attachment] = []
    for r in rows:
        if not include_plugin_payloads and is_plugin_payload(r["transfer_name"]):
            continue
        date = r["date"]
        out.append(
            row_to_attachment(
                r,
                message_rowid=int(r["mid"]),
                message_date=None if date is None else int(date),
            )
        )
    return out


def payload_for(conn: sqlite3.Connection, rowid: int) -> bytes | None:
    """Raw ``message.payload_data`` for ``rowid`` (``None`` when absent or NULL).

    Also ``None`` when the schema has no ``payload_data`` column at all
    (optional per section 4.2).
    """
    try:
        rows = conn.execute(PAYLOAD_FOR, (rowid,)).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such column" in str(exc).lower():
            return None
        raise
    if not rows:
        return None
    data = rows[0][0]
    if data is None:
        return None
    return bytes(data)
