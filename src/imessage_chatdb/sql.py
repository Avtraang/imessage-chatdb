"""SQL statements whose row order or row set is JSON-visible (DESIGN.md section 6.1).

Every constant here is the relay's statement text **verbatim** (interior
whitespace included); ``tests/test_schema.py`` compares each one against a
copy taken once from that relay (``tests/fixtures/relay_sql_golden.py``).
Changing a statement is therefore a deliberate, versioned change, never a
side effect of a refactor.  The one exception is ``SEARCH_IN_CHAT``, a
library addition derived from ``SEARCH`` at import time by inserting a single
``AND c.guid = ?`` line (``SEARCH`` itself is untouched); the tests pin that
derivation instead of a golden copy.

Statements the relay builds with an f-string keep the literal ``{ph}``
placeholder; expand it with :func:`expand_in` before executing::

    conn.execute(expand_in(ATTACHMENTS_FOR, len(rowids)), tuple(rowids))

The message SELECT itself is schema-dependent and lives in
:func:`imessage_chatdb.schema.build_message_select`; the WHERE/ORDER suffixes
appended to it are here (``AFTER_ROWID_SUFFIX`` and friends).
"""

from __future__ import annotations

__all__ = [
    "IN_PLACEHOLDER",
    "expand_in",
    "AFTER_ROWID_SUFFIX",
    "EDITED_AFTER_SUFFIX",
    "THREAD_WHERE_SUFFIX",
    "THREAD_BEFORE_SUFFIX",
    "THREAD_ORDER_SUFFIX",
    "MAX_ROWID",
    "MAX_DATE_EDITED",
    "REPLY_TARGETS",
    "ATTACHMENTS_FOR",
    "LITE_MESSAGES",
    "PARTICIPANTS",
    "LAST_ROWID",
    "FIND_1TO1",
    "FIND_GROUP_JOINS",
    "FIND_GROUP_SENDERS",
    "CHAT_SERVICES",
    "THREADS",
    "PARTICIPANTS_MAP",
    "UNREAD",
    "ONE_TO_ONE_ACTIVITY",
    "SEARCH",
    "SEARCH_IN_CHAT",
    "PAYLOAD_FOR",
    "ATTACHMENT_BY_GUID",
    "CHAT_ATTACHMENTS",
]

#: The literal token the relay's f-strings interpolate a ``?,?,...`` list into.
IN_PLACEHOLDER = "{ph}"


def expand_in(statement: str, count: int) -> str:
    """Replace ``{ph}`` with ``count`` comma-joined ``?`` marks (the relay's expression).

    ``count`` must be positive: SQLite rejects ``IN ()``, and the relay's callers
    return early on empty input, so callers here must do the same.
    """
    if count <= 0:
        raise ValueError("expand_in needs at least one value; the caller must short-circuit")
    return statement.replace(IN_PLACEHOLDER, ",".join("?" * count))


# ---- suffixes appended to build_message_select(...); see tests/fixtures/relay_sql_golden.py ----

AFTER_ROWID_SUFFIX = " WHERE m.ROWID > ? ORDER BY m.ROWID ASC"
EDITED_AFTER_SUFFIX = " WHERE m.date_edited > ? ORDER BY m.date_edited ASC"
THREAD_WHERE_SUFFIX = " WHERE c.guid = ?"
THREAD_BEFORE_SUFFIX = " AND m.ROWID < ?"
THREAD_ORDER_SUFFIX = " ORDER BY m.ROWID DESC LIMIT ?"

# ---- cursor marks ----

MAX_ROWID = "SELECT MAX(ROWID) AS m FROM message"
MAX_DATE_EDITED = "SELECT MAX(date_edited) AS m FROM message"

# ---- enrichment ----

REPLY_TARGETS = """
        SELECT m.guid AS guid, m.text AS text, m.attributedBody AS ab,
               m.is_from_me AS mine, h.id AS sender
        FROM message m LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE m.guid IN ({ph})"""

ATTACHMENTS_FOR = """SELECT maj.message_id AS mid, a.guid AS guid, a.mime_type AS mime,
                   a.transfer_name AS name
            FROM message_attachment_join maj
            JOIN attachment a ON maj.attachment_id = a.ROWID
            WHERE maj.message_id IN ({ph})"""

LITE_MESSAGES = """
        SELECT m.ROWID AS rowid, m.text AS text, m.attributedBody AS attributed_body,
               m.is_from_me AS is_from_me, m.associated_message_type AS assoc_type,
               m.cache_has_attachments AS has_attachments, h.id AS sender
        FROM message m
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE m.ROWID IN ({ph})"""

# ---- chats ----

PARTICIPANTS = """SELECT h.id AS id FROM chat_handle_join chj
           JOIN handle h ON chj.handle_id = h.ROWID WHERE chj.chat_id = ?"""

LAST_ROWID = """SELECT MAX(m.ROWID) AS m FROM chat_message_join cmj
           JOIN message m ON m.ROWID = cmj.message_id WHERE cmj.chat_id = ?"""

FIND_1TO1 = "SELECT ROWID AS rid, guid, chat_identifier AS ci FROM chat WHERE style = 45"

FIND_GROUP_JOINS = """SELECT c.ROWID AS rid, c.guid AS guid, c.display_name AS dn, h.id AS hid
           FROM chat c
           LEFT JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
           LEFT JOIN handle h ON h.ROWID = chj.handle_id
           WHERE c.style = 43"""

FIND_GROUP_SENDERS = """SELECT DISTINCT cmj.chat_id AS rid, h.id AS hid
           FROM message m
           JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
           JOIN handle h ON h.ROWID = m.handle_id
           JOIN chat c ON c.ROWID = cmj.chat_id
           WHERE c.style = 43 AND m.is_from_me = 0"""

CHAT_SERVICES = """
            SELECT c.ROWID AS rid, c.service_name AS chat_svc,
              (SELECT m.service FROM chat_message_join cmj
                 JOIN message m ON m.ROWID = cmj.message_id
                WHERE cmj.chat_id = c.ROWID AND m.is_from_me = 1
                  AND IFNULL(m.service, '') <> ''
                ORDER BY cmj.message_id DESC LIMIT 1) AS out_svc,
              (SELECT m.service FROM chat_message_join cmj
                 JOIN message m ON m.ROWID = cmj.message_id
                WHERE cmj.chat_id = c.ROWID AND IFNULL(m.service, '') <> ''
                ORDER BY cmj.message_id DESC LIMIT 1) AS any_svc
            FROM chat c WHERE c.ROWID IN ({ph})"""

THREADS = """
            SELECT c.ROWID AS chat_rowid, c.guid AS chat_guid,
                   c.display_name AS chat_name, c.chat_identifier AS chat_identifier,
                   c.style AS chat_style, MAX(m.date) AS last_date, MAX(m.ROWID) AS last_rowid
            FROM chat c
            JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
            JOIN message m ON cmj.message_id = m.ROWID
            GROUP BY c.ROWID ORDER BY last_rowid DESC LIMIT ?
        """

PARTICIPANTS_MAP = """SELECT chj.chat_id AS rid, h.id AS hid
               FROM chat_handle_join chj JOIN handle h ON h.ROWID = chj.handle_id"""

UNREAD = """SELECT COUNT(*) AS c FROM chat_message_join cmj
                       JOIN message m ON m.ROWID = cmj.message_id
                       WHERE cmj.chat_id = ? AND m.ROWID > ? AND m.is_from_me = 0"""

ONE_TO_ONE_ACTIVITY = """
            SELECT c.chat_identifier AS ci, MAX(m.ROWID) AS last
            FROM chat c
            JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
            JOIN message m ON m.ROWID = cmj.message_id
            WHERE c.style = 45 GROUP BY c.ROWID"""

# ---- search: params = (like, q, q.lower(), q.capitalize(), q.upper(), n) ----

SEARCH = """
            SELECT m.ROWID AS rowid, m.text AS text, m.attributedBody AS attributed_body,
                   m.date AS date, m.is_from_me AS is_from_me,
                   c.guid AS chat_guid, c.ROWID AS chat_rowid,
                   c.display_name AS chat_name, c.chat_identifier AS chat_identifier,
                   c.style AS chat_style, h.id AS sender
            FROM message m
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            JOIN chat c ON c.ROWID = cmj.chat_id
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            WHERE (m.text LIKE ?
                   OR instr(m.attributedBody, CAST(? AS BLOB)) > 0
                   OR instr(m.attributedBody, CAST(? AS BLOB)) > 0
                   OR instr(m.attributedBody, CAST(? AS BLOB)) > 0
                   OR instr(m.attributedBody, CAST(? AS BLOB)) > 0)
              AND IFNULL(m.associated_message_type, 0) = 0
            ORDER BY m.ROWID DESC LIMIT ?
        """

#: The tapback-exclusion line of ``SEARCH``; the chat filter is inserted before it.
_SEARCH_ASSOC_LINE = "AND IFNULL(m.associated_message_type, 0) = 0"


def _with_chat_filter(statement: str) -> str:
    """``statement`` with ``AND c.guid = ?`` inserted as the line before the tapback
    exclusion, at the same indentation.  Raises ``ValueError`` if the anchor is
    missing, so a reshaped ``SEARCH`` cannot silently produce an unfiltered search.
    """
    i = statement.index(_SEARCH_ASSOC_LINE)
    line_start = statement.rindex("\n", 0, i) + 1
    indent = statement[line_start:i]
    return statement[:line_start] + indent + "AND c.guid = ?\n" + statement[line_start:]


# ---- search within one chat (library addition, 0.1.1; not a relay statement) ----
# params = (like, q, q.lower(), q.capitalize(), q.upper(), chat_guid, n)

SEARCH_IN_CHAT = _with_chat_filter(SEARCH)

# ---- payloads and attachments ----

PAYLOAD_FOR = "SELECT payload_data FROM message WHERE ROWID = ?"

ATTACHMENT_BY_GUID = "SELECT filename, mime_type, transfer_name FROM attachment WHERE guid = ?"

CHAT_ATTACHMENTS = """
            SELECT a.guid AS guid, a.mime_type AS mime, a.transfer_name AS name,
                   m.date AS date
            FROM chat c
            JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
            JOIN message m ON cmj.message_id = m.ROWID
            JOIN message_attachment_join maj ON m.ROWID = maj.message_id
            JOIN attachment a ON maj.attachment_id = a.ROWID
            WHERE c.guid = ?
            ORDER BY m.date DESC
        """
