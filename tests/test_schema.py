"""Tests for ``imessage_chatdb.schema`` and ``imessage_chatdb.sql`` (DESIGN.md 8.4 schema)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from imessage_chatdb import schema as schema_mod
from imessage_chatdb import sql
from imessage_chatdb.errors import SchemaError
from imessage_chatdb.schema import (
    MESSAGE_SELECT_ALIASES,
    OPTIONAL,
    REQUIRED,
    TABLES,
    Schema,
    build_message_select,
)
from tests.conftest import FixtureDB, MakeDB, assert_safe_db_path
from tests.fixtures import builders
from tests.fixtures import relay_sql_golden as golden
from tests.fixtures.schema_profiles import PROFILES, column_names, missing_columns

# ---------------------------------------------------------------------------
# sql.py: byte-equality with the relay's statement text
# ---------------------------------------------------------------------------

# Golden names that are not sql.py constants (they are checked elsewhere).
_NOT_IN_SQL_MODULE = {"MESSAGE_SELECT", "LINK_BALLOON"}


def _sql_constants() -> list[str]:
    return [n for n in sql.__all__ if n.isupper() and n != "IN_PLACEHOLDER"]


def test_every_sql_constant_has_a_golden_entry() -> None:
    assert set(_sql_constants()) == set(golden.GOLDEN) - _NOT_IN_SQL_MODULE


@pytest.mark.parametrize("name", sorted(set(golden.GOLDEN) - _NOT_IN_SQL_MODULE))
def test_sql_constant_is_verbatim(name: str) -> None:
    where, expected = golden.GOLDEN[name]
    actual = getattr(sql, name)
    # Exact text after stripping ONLY leading/trailing whitespace: interior
    # spacing, newlines and indentation must match the relay byte for byte.
    assert actual.strip() == expected.strip(), f"{name} differs from {where}"


def test_golden_entries_carry_a_description_and_text() -> None:
    for name, (where, text) in golden.GOLDEN.items():
        assert where and where.strip() == where and "relay.py" not in where, name
        assert text, name


def test_expand_in_matches_relays_expression() -> None:
    for n in (1, 2, 7):
        ph = ",".join("?" * n)
        assert sql.expand_in(sql.ATTACHMENTS_FOR, n) == sql.ATTACHMENTS_FOR.replace("{ph}", ph)
    assert "{ph}" not in sql.expand_in(sql.CHAT_SERVICES, 3)
    assert sql.expand_in(sql.REPLY_TARGETS, 2).endswith("IN (?,?)")


def test_expand_in_rejects_empty() -> None:
    with pytest.raises(ValueError):
        sql.expand_in(sql.LITE_MESSAGES, 0)


def test_in_statements_carry_placeholder() -> None:
    for name in ("REPLY_TARGETS", "ATTACHMENTS_FOR", "LITE_MESSAGES", "CHAT_SERVICES"):
        assert sql.IN_PLACEHOLDER in getattr(sql, name), name
    for name in _sql_constants():
        if name not in ("REPLY_TARGETS", "ATTACHMENTS_FOR", "LITE_MESSAGES", "CHAT_SERVICES"):
            assert sql.IN_PLACEHOLDER not in getattr(sql, name), name


def test_link_balloon_literal_matches_relay() -> None:
    assert schema_mod._LINK_BALLOON == golden.LINK_BALLOON


@pytest.mark.parametrize("name", _sql_constants())
def test_every_statement_prepares_on_newest_profile(make_db: MakeDB, name: str) -> None:
    """Each statement compiles against the macos27 DDL (catches a typo in a column name)."""
    fx = make_db("macos27")
    stmt = getattr(sql, name)
    if name == "THREAD_BEFORE_SUFFIX":
        stmt = build_message_select(Schema(fx.writer)) + sql.THREAD_WHERE_SUFFIX + stmt
    elif name.endswith("_SUFFIX"):
        stmt = build_message_select(Schema(fx.writer)) + stmt
    if sql.IN_PLACEHOLDER in stmt:
        stmt = sql.expand_in(stmt, 1)
    # EXPLAIN compiles without running the query; bind a NULL per parameter.
    fx.writer.execute("EXPLAIN " + stmt, (None,) * stmt.count("?")).fetchall()


# ---------------------------------------------------------------------------
# Schema introspection
# ---------------------------------------------------------------------------


def test_required_and_optional_are_disjoint_and_cover_tables() -> None:
    assert set(REQUIRED) == set(TABLES)
    for table, cols in OPTIONAL.items():
        assert table in REQUIRED
        assert not set(cols) & set(REQUIRED[table]), table
        assert len(cols) == len(set(cols)), table


def test_profile_detection(fixture_db: FixtureDB) -> None:
    s = Schema(fixture_db.writer)
    assert s.profile == fixture_db.profile


def test_optional_missing_matches_fixture_profile(fixture_db: FixtureDB) -> None:
    s = Schema(fixture_db.writer)
    assert s.optional_missing == missing_columns(fixture_db.profile)
    for entry in s.optional_missing:
        table, column = entry.split(".")
        assert column in OPTIONAL[table]
        assert not s.has(table, column)


def test_columns_dict_matches_ddl(fixture_db: FixtureDB) -> None:
    s = Schema(fixture_db.writer)
    expected = column_names(fixture_db.profile)
    assert {t: list(cols) for t, cols in s.columns.items()} == expected
    assert s.columns["message"]["attributedBody"] == "BLOB"
    assert s.columns["message"]["guid"] == "TEXT"


def test_has_is_case_insensitive_and_rowid_always_present(fixture_db: FixtureDB) -> None:
    s = Schema(fixture_db.writer)
    assert s.has("message", "attributedBody")
    assert s.has("message", "attributedbody")
    assert s.has("message", "ROWID") and s.has("chat", "rowid")
    assert not s.has("message", "no_such_column")
    assert not s.has("no_such_table", "ROWID")
    assert not s.has_table("no_such_table")


def test_check_passes_on_every_profile(fixture_db: FixtureDB) -> None:
    Schema(fixture_db.writer).check()


def test_missing_required_column_raises_schema_error(tmp_path: Path) -> None:
    path = assert_safe_db_path(tmp_path / "noguid.db", tmp_path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT)")
        for t in TABLES[1:]:
            conn.execute(f"CREATE TABLE {t} (x INTEGER)")
        s = Schema(conn)
        with pytest.raises(SchemaError) as ei:
            s.check()
        assert ei.value.column == "message.guid"
        with pytest.raises(SchemaError):
            build_message_select(s)
    finally:
        conn.close()


def test_missing_table_gives_unknown_profile_and_fails_check(tmp_path: Path) -> None:
    path = assert_safe_db_path(tmp_path / "notables.db", tmp_path)
    conn = sqlite3.connect(path)
    try:
        s = Schema(conn)
        assert s.profile == "unknown"
        assert s.columns["message"] == {}
        with pytest.raises(SchemaError) as ei:
            s.check()
        assert ei.value.column == "message.ROWID"
    finally:
        conn.close()


def test_profile_heuristic_on_hand_built_tables(tmp_path: Path) -> None:
    """Pins the order of the checks: 27 markers > emoji > thread part > macos14."""

    counter = 0

    def build(extra_message_cols: list[str]) -> str:
        nonlocal counter
        counter += 1
        p = assert_safe_db_path(tmp_path / f"heuristic-{counter}.db", tmp_path)
        conn = sqlite3.connect(p)
        try:
            cols = ", ".join(["ROWID INTEGER PRIMARY KEY", *extra_message_cols])
            conn.execute(f"CREATE TABLE message ({cols})")
            for t in TABLES[1:]:
                conn.execute(f"CREATE TABLE {t} (x INTEGER)")
            return Schema(conn).profile
        finally:
            conn.close()

    assert build([]) == "macos14"
    assert build(["thread_originator_part TEXT"]) == "macos15"
    assert build(["associated_message_emoji TEXT"]) == "macos26"
    assert build(["is_spam INTEGER"]) == "macos27"
    assert build(["group_title TEXT"]) == "macos27"
    assert build(["date_updated INTEGER", "associated_message_emoji TEXT"]) == "macos27"


def test_repr_mentions_profile(fixture_db: FixtureDB) -> None:
    assert fixture_db.profile in repr(Schema(fixture_db.writer))


def test_select_or_null(fixture_db: FixtureDB) -> None:
    s = Schema(fixture_db.writer)
    assert s.select_or_null("m", "message", "guid", "guid") == "m.guid AS guid"
    assert s.select_or_null("m", "message", "nonexistent", "x") == "NULL AS x"


# ---------------------------------------------------------------------------
# build_message_select
# ---------------------------------------------------------------------------

_FROM_BLOCK = (
    "FROM message m\n"
    "LEFT JOIN handle h ON m.handle_id = h.ROWID\n"
    "JOIN chat_message_join cmj ON m.ROWID = cmj.message_id\n"
    "JOIN chat c ON cmj.chat_id = c.ROWID\n"
)


def _aliases(conn: sqlite3.Connection, select: str) -> list[str]:
    cur = conn.execute(select + " LIMIT 0")
    return [d[0] for d in cur.description]


def test_select_aliases_are_stable_on_every_profile(fixture_db: FixtureDB) -> None:
    s = Schema(fixture_db.writer)
    for orphans in (False, True):
        select = build_message_select(s, include_orphans=orphans)
        assert _aliases(fixture_db.writer, select) == list(MESSAGE_SELECT_ALIASES)


def test_select_from_block_is_relays_verbatim(fixture_db: FixtureDB) -> None:
    select = build_message_select(Schema(fixture_db.writer))
    assert select.endswith(_FROM_BLOCK)
    assert select.startswith("\nSELECT\n")
    # the relay's FROM/JOIN block, byte for byte
    relay_from = golden.MESSAGE_SELECT[golden.MESSAGE_SELECT.index("FROM message m") :]
    assert _FROM_BLOCK == relay_from


def test_select_covers_every_relay_alias() -> None:
    relay_select = golden.MESSAGE_SELECT
    for alias in (
        "rowid", "guid", "text", "attributed_body", "date", "is_from_me", "date_read",
        "date_edited", "assoc_guid", "assoc_type", "has_attachments", "payload_data",
        "balloon_bundle_id", "reply_to_guid", "service", "sender", "chat_guid", "chat_name",
        "chat_identifier", "chat_style",
    ):  # fmt: skip
        assert f" AS {alias}" in relay_select, alias
        assert alias in MESSAGE_SELECT_ALIASES, alias


def test_include_orphans_uses_left_joins(fixture_db: FixtureDB) -> None:
    s = Schema(fixture_db.writer)
    strict = build_message_select(s)
    loose = build_message_select(s, include_orphans=True)
    assert "\nJOIN chat_message_join cmj" in strict and "\nJOIN chat c ON" in strict
    assert "\nLEFT JOIN chat_message_join cmj" in loose and "\nLEFT JOIN chat c ON" in loose
    assert strict.count("LEFT JOIN") == 1 and loose.count("LEFT JOIN") == 3


def test_macos14_renders_null_for_every_absent_optional(make_db: MakeDB) -> None:
    fx = make_db("macos14")
    select = build_message_select(Schema(fx.writer))
    for alias in ("date_retracted", "assoc_emoji", "reply_to_part", "filter_action",
                  "is_spam", "group_title"):  # fmt: skip
        assert f"NULL AS {alias}" in select, alias
    # present optional columns are selected from the table, not NULL
    for alias in ("date_read", "date_delivered", "date_edited", "reply_to_guid", "service",
                  "item_type", "group_action_type", "expressive_send_style_id",
                  "balloon_bundle_id"):  # fmt: skip
        assert f"m.{_source_column(alias)} AS {alias}" in select, alias
        assert f"NULL AS {alias}" not in select, alias


def _source_column(alias: str) -> str:
    return {"reply_to_guid": "thread_originator_guid", "reply_to_part": "thread_originator_part",
            "assoc_emoji": "associated_message_emoji"}.get(alias, alias)  # fmt: skip


def test_macos27_renders_no_nulls(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    select = build_message_select(Schema(fx.writer))
    assert "NULL AS" not in select


def test_payload_case_when_trim(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    select = build_message_select(Schema(fx.writer))
    assert (
        "CASE WHEN m.balloon_bundle_id = 'com.apple.messages.URLBalloonProvider' "
        "THEN m.payload_data END AS payload_data"
    ) in select


def test_payload_trim_is_output_neutral(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    w = fx.writer
    cid = builders.add_chat(w, "any;-;+15550001234", 45)
    link = builders.add_message(
        w, cid, text="x", balloon="com.apple.messages.URLBalloonProvider", payload=b"LINK"
    )
    other = builders.add_message(w, cid, text="y", balloon="com.apple.other", payload=b"OTHER")
    plain = builders.add_message(w, cid, text="z", payload=b"PLAIN")
    rows = {
        r[0]: (r[1], r[2])
        for r in w.execute(
            "SELECT rowid, balloon_bundle_id, payload_data FROM ("
            + build_message_select(Schema(w))
            + ") ORDER BY rowid"
        ).fetchall()
    }
    assert rows[link] == ("com.apple.messages.URLBalloonProvider", b"LINK")
    assert rows[other] == ("com.apple.other", None)
    assert rows[plain] == (None, None)


def _schema_without(tmp_path: Path, name: str, drop: set[str]) -> tuple[sqlite3.Connection, Schema]:
    """A macos27-shaped DB whose ``message`` table lacks ``drop``."""
    from tests.fixtures.schema_profiles import columns

    path = assert_safe_db_path(tmp_path / name, tmp_path)
    conn = sqlite3.connect(path, isolation_level=None)
    for table, cols in columns("macos27").items():
        body = ", ".join(f"{c} {d}" for c, d in cols if not (table == "message" and c in drop))
        conn.execute(f"CREATE TABLE {table} ({body})")
    return conn, Schema(conn)


def test_payload_plain_when_balloon_column_absent(tmp_path: Path) -> None:
    conn, s = _schema_without(tmp_path, "noballoon.db", {"balloon_bundle_id"})
    try:
        select = build_message_select(s)
        assert "m.payload_data AS payload_data" in select
        assert "CASE WHEN" not in select
        assert "NULL AS balloon_bundle_id" in select
        assert _aliases(conn, select) == list(MESSAGE_SELECT_ALIASES)
    finally:
        conn.close()


def test_payload_null_when_payload_column_absent(tmp_path: Path) -> None:
    conn, s = _schema_without(tmp_path, "nopayload.db", {"payload_data"})
    try:
        select = build_message_select(s)
        assert "NULL AS payload_data" in select
        assert "CASE WHEN" not in select
        assert _aliases(conn, select) == list(MESSAGE_SELECT_ALIASES)
    finally:
        conn.close()


def test_select_runs_and_hides_orphans_by_default(fixture_db: FixtureDB) -> None:
    w = fixture_db.writer
    hid = builders.add_handle(w, "+15550001234")
    cid = builders.add_chat(w, "any;-;+15550001234", 45, handles=[hid])
    joined = builders.add_message(w, cid, text="joined", handle=hid)
    orphan = builders.add_message(w, cid, text="orphan", join=False)
    s = Schema(w)

    rows = w.execute(build_message_select(s) + sql.AFTER_ROWID_SUFFIX, (0,)).fetchall()
    assert [r[0] for r in rows] == [joined]

    rows = w.execute(
        build_message_select(s, include_orphans=True) + sql.AFTER_ROWID_SUFFIX, (0,)
    ).fetchall()
    by_rowid = {r[0]: r for r in rows}
    assert sorted(by_rowid) == [joined, orphan]
    w.row_factory = sqlite3.Row
    row = w.execute(
        build_message_select(s, include_orphans=True) + " WHERE m.ROWID = ?", (orphan,)
    ).fetchone()
    assert row["chat_rowid"] is None and row["chat_guid"] is None
    assert row["text"] == "orphan"
    jrow = w.execute(build_message_select(s) + " WHERE m.ROWID = ?", (joined,)).fetchone()
    assert jrow["chat_rowid"] == cid and jrow["sender"] == "+15550001234"
    assert jrow["handle_rowid"] == hid
    # optional columns the profile lacks read back as None
    for entry in s.optional_missing:
        table, column = entry.split(".")
        if table == "message" and column in dict(_ALIAS_BY_COLUMN):
            assert jrow[dict(_ALIAS_BY_COLUMN)[column]] is None, column


_ALIAS_BY_COLUMN: tuple[tuple[str, str], ...] = (
    ("date_retracted", "date_retracted"),
    ("associated_message_emoji", "assoc_emoji"),
    ("thread_originator_part", "reply_to_part"),
    ("filter_action", "filter_action"),
    ("is_spam", "is_spam"),
    ("group_title", "group_title"),
)


def test_suffixes_compose_into_relay_statements(fixture_db: FixtureDB) -> None:
    w = fixture_db.writer
    cid = builders.add_chat(w, "any;+;chat1", 43, display_name="G")
    ids = [builders.add_message(w, cid, text=f"m{i}") for i in range(5)]
    s = Schema(w)
    base = build_message_select(s)

    newer = w.execute(base + sql.AFTER_ROWID_SUFFIX, (ids[1],)).fetchall()
    assert [r[0] for r in newer] == ids[2:]

    page = w.execute(
        base + sql.THREAD_WHERE_SUFFIX + sql.THREAD_BEFORE_SUFFIX + sql.THREAD_ORDER_SUFFIX,
        ("any;+;chat1", ids[3], 2),
    ).fetchall()
    assert [r[0] for r in page] == [ids[2], ids[1]]

    edited = w.execute(base + sql.EDITED_AFTER_SUFFIX, (0,)).fetchall()
    assert edited == []  # date_edited is 0 on every row (never NULL)


def test_profiles_constant_matches_fixtures() -> None:
    assert schema_mod.PROFILES == PROFILES
