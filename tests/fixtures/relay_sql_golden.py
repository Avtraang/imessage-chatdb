"""Golden copies of the SQL statements whose row order or row set is JSON-visible (T4).

Each constant is the exact text of the original string literal in the HTTP
relay this library was extracted from (f-string ``{ph}`` placeholders kept
literally), copied once and never edited.  ``tests/test_schema.py`` asserts
that every ``imessage_chatdb.sql`` constant equals its entry here after
stripping only leading and trailing whitespace.  Each ``GOLDEN`` entry is
``(description, text)``; the description names the statement's role.
"""

from __future__ import annotations

GOLDEN: dict[str, tuple[str, str]] = {}


# message select (joins and aliases)
MESSAGE_SELECT = """
SELECT
    m.ROWID AS rowid, m.guid AS guid, m.text AS text,
    m.attributedBody AS attributed_body, m.date AS date, m.is_from_me AS is_from_me,
    m.date_read AS date_read, m.date_edited AS date_edited,
    m.associated_message_guid AS assoc_guid,
    m.associated_message_type AS assoc_type, m.cache_has_attachments AS has_attachments,
    m.payload_data AS payload_data, m.balloon_bundle_id AS balloon_bundle_id,
    m.thread_originator_guid AS reply_to_guid, m.service AS service,
    h.id AS sender, c.guid AS chat_guid, c.display_name AS chat_name,
    c.chat_identifier AS chat_identifier, c.style AS chat_style
FROM message m
LEFT JOIN handle h ON m.handle_id = h.ROWID
JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
JOIN chat c ON cmj.chat_id = c.ROWID
"""
GOLDEN["MESSAGE_SELECT"] = ("message select (joins and aliases)", MESSAGE_SELECT)

# URL-balloon bundle id literal
LINK_BALLOON = "com.apple.messages.URLBalloonProvider"
GOLDEN["LINK_BALLOON"] = ("URL-balloon bundle id literal", LINK_BALLOON)

# new-message tail suffix
AFTER_ROWID_SUFFIX = " WHERE m.ROWID > ? ORDER BY m.ROWID ASC"
GOLDEN["AFTER_ROWID_SUFFIX"] = ("new-message tail suffix", AFTER_ROWID_SUFFIX)

# ROWID high-water mark
MAX_ROWID = "SELECT MAX(ROWID) AS m FROM message"
GOLDEN["MAX_ROWID"] = ("ROWID high-water mark", MAX_ROWID)

# date_edited high-water mark
MAX_DATE_EDITED = "SELECT MAX(date_edited) AS m FROM message"
GOLDEN["MAX_DATE_EDITED"] = ("date_edited high-water mark", MAX_DATE_EDITED)

# edited-message tail suffix
EDITED_AFTER_SUFFIX = " WHERE m.date_edited > ? ORDER BY m.date_edited ASC"
GOLDEN["EDITED_AFTER_SUFFIX"] = ("edited-message tail suffix", EDITED_AFTER_SUFFIX)

# thread page: chat filter
THREAD_WHERE_SUFFIX = " WHERE c.guid = ?"
GOLDEN["THREAD_WHERE_SUFFIX"] = ("thread page: chat filter", THREAD_WHERE_SUFFIX)

# thread page: before-ROWID bound
THREAD_BEFORE_SUFFIX = " AND m.ROWID < ?"
GOLDEN["THREAD_BEFORE_SUFFIX"] = ("thread page: before-ROWID bound", THREAD_BEFORE_SUFFIX)

# thread page: order and limit
THREAD_ORDER_SUFFIX = " ORDER BY m.ROWID DESC LIMIT ?"
GOLDEN["THREAD_ORDER_SUFFIX"] = ("thread page: order and limit", THREAD_ORDER_SUFFIX)

# reply targets by guid
REPLY_TARGETS = """
        SELECT m.guid AS guid, m.text AS text, m.attributedBody AS ab,
               m.is_from_me AS mine, h.id AS sender
        FROM message m LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE m.guid IN ({ph})"""
GOLDEN["REPLY_TARGETS"] = ("reply targets by guid", REPLY_TARGETS)

# attachments of a set of messages
ATTACHMENTS_FOR = """SELECT maj.message_id AS mid, a.guid AS guid, a.mime_type AS mime,
                   a.transfer_name AS name
            FROM message_attachment_join maj
            JOIN attachment a ON maj.attachment_id = a.ROWID
            WHERE maj.message_id IN ({ph})"""
GOLDEN["ATTACHMENTS_FOR"] = ("attachments of a set of messages", ATTACHMENTS_FOR)

# preview facts of a set of messages
LITE_MESSAGES = """
        SELECT m.ROWID AS rowid, m.text AS text, m.attributedBody AS attributed_body,
               m.is_from_me AS is_from_me, m.associated_message_type AS assoc_type,
               m.cache_has_attachments AS has_attachments, h.id AS sender
        FROM message m
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE m.ROWID IN ({ph})"""
GOLDEN["LITE_MESSAGES"] = ("preview facts of a set of messages", LITE_MESSAGES)

# participants of one chat
PARTICIPANTS = """SELECT h.id AS id FROM chat_handle_join chj
           JOIN handle h ON chj.handle_id = h.ROWID WHERE chj.chat_id = ?"""
GOLDEN["PARTICIPANTS"] = ("participants of one chat", PARTICIPANTS)

# last ROWID of one chat
LAST_ROWID = """SELECT MAX(m.ROWID) AS m FROM chat_message_join cmj
           JOIN message m ON m.ROWID = cmj.message_id WHERE cmj.chat_id = ?"""
GOLDEN["LAST_ROWID"] = ("last ROWID of one chat", LAST_ROWID)

# one-to-one chat candidates
FIND_1TO1 = "SELECT ROWID AS rid, guid, chat_identifier AS ci FROM chat WHERE style = 45"
GOLDEN["FIND_1TO1"] = ("one-to-one chat candidates", FIND_1TO1)

# group chat candidates via join rows
FIND_GROUP_JOINS = """SELECT c.ROWID AS rid, c.guid AS guid, c.display_name AS dn, h.id AS hid
           FROM chat c
           LEFT JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
           LEFT JOIN handle h ON h.ROWID = chj.handle_id
           WHERE c.style = 43"""
GOLDEN["FIND_GROUP_JOINS"] = ("group chat candidates via join rows", FIND_GROUP_JOINS)

# group chat candidates via incoming senders
FIND_GROUP_SENDERS = """SELECT DISTINCT cmj.chat_id AS rid, h.id AS hid
           FROM message m
           JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
           JOIN handle h ON h.ROWID = m.handle_id
           JOIN chat c ON c.ROWID = cmj.chat_id
           WHERE c.style = 43 AND m.is_from_me = 0"""
GOLDEN["FIND_GROUP_SENDERS"] = ("group chat candidates via incoming senders", FIND_GROUP_SENDERS)

# per-chat service (outgoing, any)
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
GOLDEN["CHAT_SERVICES"] = ("per-chat service (outgoing, any)", CHAT_SERVICES)

# chats by last activity
THREADS = """
            SELECT c.ROWID AS chat_rowid, c.guid AS chat_guid,
                   c.display_name AS chat_name, c.chat_identifier AS chat_identifier,
                   c.style AS chat_style, MAX(m.date) AS last_date, MAX(m.ROWID) AS last_rowid
            FROM chat c
            JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
            JOIN message m ON cmj.message_id = m.ROWID
            GROUP BY c.ROWID ORDER BY last_rowid DESC LIMIT ?
        """
GOLDEN["THREADS"] = ("chats by last activity", THREADS)

# participants of every chat
PARTICIPANTS_MAP = """SELECT chj.chat_id AS rid, h.id AS hid
               FROM chat_handle_join chj JOIN handle h ON h.ROWID = chj.handle_id"""
GOLDEN["PARTICIPANTS_MAP"] = ("participants of every chat", PARTICIPANTS_MAP)

# incoming messages after a ROWID
UNREAD = """SELECT COUNT(*) AS c FROM chat_message_join cmj
                       JOIN message m ON m.ROWID = cmj.message_id
                       WHERE cmj.chat_id = ? AND m.ROWID > ? AND m.is_from_me = 0"""
GOLDEN["UNREAD"] = ("incoming messages after a ROWID", UNREAD)

# last ROWID of every one-to-one chat
ONE_TO_ONE_ACTIVITY = """
            SELECT c.chat_identifier AS ci, MAX(m.ROWID) AS last
            FROM chat c
            JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
            JOIN message m ON m.ROWID = cmj.message_id
            WHERE c.style = 45 GROUP BY c.ROWID"""
GOLDEN["ONE_TO_ONE_ACTIVITY"] = ("last ROWID of every one-to-one chat", ONE_TO_ONE_ACTIVITY)

# text search (LIKE plus instr variants)
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
GOLDEN["SEARCH"] = ("text search (LIKE plus instr variants)", SEARCH)

# payload_data of one message
PAYLOAD_FOR = "SELECT payload_data FROM message WHERE ROWID = ?"
GOLDEN["PAYLOAD_FOR"] = ("payload_data of one message", PAYLOAD_FOR)

# one attachment by guid
ATTACHMENT_BY_GUID = "SELECT filename, mime_type, transfer_name FROM attachment WHERE guid = ?"
GOLDEN["ATTACHMENT_BY_GUID"] = ("one attachment by guid", ATTACHMENT_BY_GUID)

# attachments of one chat, newest first
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
GOLDEN["CHAT_ATTACHMENTS"] = ("attachments of one chat, newest first", CHAT_ATTACHMENTS)
