"""Schema introspection and the schema-dependent message SELECT (DESIGN.md 4.2, 6.1).

``chat.db`` gains and loses columns across macOS releases (macOS 27 reordered
``message`` and dropped every ``sr_*`` column), so the library reads columns by
name only and asks SQLite which optional columns exist before building the
message SELECT.  Missing optional columns render as ``NULL AS <alias>`` so the
row shape - and therefore ``row_to_message`` - never changes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from .errors import SchemaError

__all__ = [
    "TABLES",
    "REQUIRED",
    "OPTIONAL",
    "PROFILES",
    "MESSAGE_SELECT_ALIASES",
    "Schema",
    "build_message_select",
]

#: The tables ``Schema`` introspects, in the order ``check()`` reports them.
TABLES: tuple[str, ...] = (
    "message",
    "chat",
    "handle",
    "attachment",
    "chat_message_join",
    "chat_handle_join",
    "message_attachment_join",
)

REQUIRED: Mapping[str, list[str]] = {
    "message": [
        "ROWID",
        "guid",
        "text",
        "attributedBody",
        "date",
        "is_from_me",
        "handle_id",
        "cache_has_attachments",
        "associated_message_guid",
        "associated_message_type",
    ],
    "chat": ["ROWID", "guid", "style", "chat_identifier", "display_name"],
    "handle": ["ROWID", "id"],
    "attachment": ["ROWID", "guid", "filename", "mime_type", "transfer_name"],
    "chat_message_join": ["chat_id", "message_id"],
    "chat_handle_join": ["chat_id", "handle_id"],
    "message_attachment_join": ["message_id", "attachment_id"],
}

OPTIONAL: Mapping[str, list[str]] = {
    "message": [
        "date_read",
        "date_delivered",
        "date_edited",
        "date_retracted",
        "date_updated",
        "service",
        "associated_message_emoji",
        "balloon_bundle_id",
        "payload_data",
        "thread_originator_guid",
        "thread_originator_part",
        "item_type",
        "group_action_type",
        "group_title",
        "filter_action",
        "is_spam",
        "expressive_send_style_id",
    ],
    "chat": [
        "service_name",
        "is_archived",
        "is_filtered",
        "group_id",
        "last_read_message_timestamp",
    ],
    "handle": ["service", "country", "uncanonicalized_id", "person_centric_id"],
    "attachment": ["uti", "total_bytes", "is_sticker", "hide_attachment", "created_date"],
}

#: Best-effort profile names, oldest first.
PROFILES: tuple[str, ...] = ("macos14", "macos15", "macos26", "macos27")

# The literal that link-preview parsing is gated on.  Repeated
# here rather than imported from link_preview so schema stays a leaf module.
_LINK_BALLOON = "com.apple.messages.URLBalloonProvider"

#: Column aliases of ``build_message_select`` in SELECT order; ``row_to_message``
#: reads rows by these names.
MESSAGE_SELECT_ALIASES: tuple[str, ...] = (
    "rowid",
    "guid",
    "text",
    "attributed_body",
    "date",
    "is_from_me",
    "handle_rowid",
    "date_read",
    "date_delivered",
    "date_edited",
    "date_retracted",
    "assoc_guid",
    "assoc_type",
    "assoc_emoji",
    "has_attachments",
    "balloon_bundle_id",
    "payload_data",
    "reply_to_guid",
    "reply_to_part",
    "service",
    "item_type",
    "group_action_type",
    "group_title",
    "filter_action",
    "is_spam",
    "expressive_send_style_id",
    "sender",
    "chat_rowid",
    "chat_guid",
    "chat_name",
    "chat_identifier",
    "chat_style",
)

# (column, alias) for the optional message columns, in SELECT order.
_OPTIONAL_MESSAGE_SIMPLE: tuple[tuple[str, str], ...] = (
    ("date_read", "date_read"),
    ("date_delivered", "date_delivered"),
    ("date_edited", "date_edited"),
    ("date_retracted", "date_retracted"),
)
_OPTIONAL_MESSAGE_TAIL: tuple[tuple[str, str], ...] = (
    ("service", "service"),
    ("item_type", "item_type"),
    ("group_action_type", "group_action_type"),
    ("group_title", "group_title"),
    ("filter_action", "filter_action"),
    ("is_spam", "is_spam"),
    ("expressive_send_style_id", "expressive_send_style_id"),
)


class Schema:
    """What ``PRAGMA table_info`` says about the seven tables the library reads.

    Introspection happens once, in ``__init__``; nothing is cached across
    connections, so a ``Schema`` describes the database as it was when built
    (``ChatDB.refresh_schema()`` rebuilds it).

    Column lookups are case-insensitive, as SQLite's are.  ``ROWID`` counts as
    present on every existing table (it is SQLite's implicit key even when the
    DDL does not declare it, and chat.db declares it explicitly).
    """

    __slots__ = ("_lower", "columns", "optional_missing", "profile")

    columns: dict[str, dict[str, str]]
    optional_missing: frozenset[str]
    profile: str

    def __init__(self, conn: sqlite3.Connection) -> None:
        columns: dict[str, dict[str, str]] = {}
        lower: dict[str, dict[str, str]] = {}
        for table in TABLES:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            cols: dict[str, str] = {}
            for row in rows:
                # row: (cid, name, type, notnull, dflt_value, pk) - works for
                # tuples and sqlite3.Row alike.
                name = str(row[1])
                decl = "" if row[2] is None else str(row[2])
                cols[name] = decl
            columns[table] = cols
            lower[table] = {name.lower(): name for name in cols}
        self.columns = columns
        self._lower = lower
        self.optional_missing = frozenset(
            f"{table}.{column}"
            for table, names in OPTIONAL.items()
            for column in names
            if not self.has(table, column)
        )
        self.profile = self._detect_profile()

    # -- queries -----------------------------------------------------------

    def has(self, table: str, column: str) -> bool:
        """True when ``table`` exists and has ``column`` (case-insensitive)."""
        names = self._lower.get(table)
        if not names:
            return False
        if column.lower() == "rowid":
            return True
        return column.lower() in names

    def has_table(self, table: str) -> bool:
        """True when ``PRAGMA table_info`` returned at least one column for ``table``."""
        return bool(self.columns.get(table))

    def check(self) -> None:
        """Raise ``SchemaError("table.column")`` for the first missing required column.

        Tables are visited in ``TABLES`` order and columns in ``REQUIRED`` order,
        so the reported name is deterministic.
        """
        for table in TABLES:
            for column in REQUIRED.get(table, ()):
                if not self.has(table, column):
                    raise SchemaError(f"{table}.{column}")

    def select_or_null(self, prefix: str, table: str, column: str, alias: str) -> str:
        """``"<prefix>.<column> AS <alias>"`` when present, else ``"NULL AS <alias>"``.

        The building block of :func:`build_message_select`; other modules may
        use it for their own schema-tolerant selects.
        """
        if self.has(table, column):
            return f"{prefix}.{column} AS {alias}"
        return f"NULL AS {alias}"

    # -- internals ---------------------------------------------------------

    def _detect_profile(self) -> str:
        if not all(self.has_table(t) for t in TABLES):
            return "unknown"
        if any(self.has("message", c) for c in ("date_updated", "is_spam", "group_title")):
            return "macos27"
        if self.has("message", "associated_message_emoji"):
            return "macos26"
        if self.has("message", "thread_originator_part"):
            return "macos15"
        return "macos14"

    def __repr__(self) -> str:
        return (
            f"Schema(profile={self.profile!r}, "
            f"optional_missing={sorted(self.optional_missing)!r})"
        )


def build_message_select(schema: Schema, *, include_orphans: bool = False) -> str:
    """Render the message SELECT of DESIGN.md section 6.1 for ``schema``.

    - Every optional column that ``schema`` lacks becomes ``NULL AS <alias>``.
    - ``payload_data`` is trimmed to URL-balloon rows with
      ``CASE WHEN m.balloon_bundle_id = '...URLBalloonProvider' THEN m.payload_data END``
      (output-neutral: callers only parse it for that balloon).  Without a
      ``balloon_bundle_id`` column the payload is selected plain; without a
      ``payload_data`` column it is ``NULL``.
    - ``include_orphans`` turns the ``chat_message_join`` / ``chat`` joins into
      ``LEFT JOIN`` so messages without a chat row are returned (their ``chat_*``
      aliases are then ``NULL``).

    The text starts with a newline and ends with one, like the golden
    ``MESSAGE_SELECT`` in ``tests/fixtures/relay_sql_golden.py``, so
    ``build_message_select(s) + sql.AFTER_ROWID_SUFFIX`` is a complete statement.
    Raises ``SchemaError`` when a required column is missing (``schema.check()``
    is called first).
    """
    schema.check()
    opt = schema.select_or_null

    simple = " ".join(
        opt("m", "message", col, alias) + "," for col, alias in _OPTIONAL_MESSAGE_SIMPLE
    )
    emoji = opt("m", "message", "associated_message_emoji", "assoc_emoji")
    balloon = opt("m", "message", "balloon_bundle_id", "balloon_bundle_id")

    if not schema.has("message", "payload_data"):
        payload = "NULL AS payload_data"
    elif schema.has("message", "balloon_bundle_id"):
        payload = (
            f"CASE WHEN m.balloon_bundle_id = '{_LINK_BALLOON}' "
            "THEN m.payload_data END AS payload_data"
        )
    else:
        payload = "m.payload_data AS payload_data"

    reply_guid = opt("m", "message", "thread_originator_guid", "reply_to_guid")
    reply_part = opt("m", "message", "thread_originator_part", "reply_to_part")
    tail = " ".join(opt("m", "message", col, alias) + "," for col, alias in _OPTIONAL_MESSAGE_TAIL)

    join = "LEFT JOIN" if include_orphans else "JOIN"

    return (
        "\n"
        "SELECT\n"
        "    m.ROWID AS rowid, m.guid AS guid, m.text AS text,\n"
        "    m.attributedBody AS attributed_body, m.date AS date, m.is_from_me AS is_from_me,\n"
        "    m.handle_id AS handle_rowid,\n"
        f"    {simple}\n"
        "    m.associated_message_guid AS assoc_guid, m.associated_message_type AS assoc_type,\n"
        f"    {emoji},\n"
        "    m.cache_has_attachments AS has_attachments,\n"
        f"    {balloon},\n"
        f"    {payload},\n"
        f"    {reply_guid}, {reply_part},\n"
        f"    {tail}\n"
        "    h.id AS sender, c.ROWID AS chat_rowid, c.guid AS chat_guid,"
        " c.display_name AS chat_name,\n"
        "    c.chat_identifier AS chat_identifier, c.style AS chat_style\n"
        "FROM message m\n"
        "LEFT JOIN handle h ON m.handle_id = h.ROWID\n"
        f"{join} chat_message_join cmj ON m.ROWID = cmj.message_id\n"
        f"{join} chat c ON cmj.chat_id = c.ROWID\n"
    )
