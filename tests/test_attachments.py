"""Tests for ``imessage_chatdb.attachments`` (DESIGN.md 8.4 attachments, T6).

Pinned: the default exclusion is the ``.pluginPayloadAttachment`` suffix (a
hidden-but-not-plugin PNG is returned by default); ``include_plugin_payloads``
turns it off; ``path`` expands ``~`` and is ``None`` for NULL/empty filenames;
``chat_attachments`` orders by ``m.date DESC``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from imessage_chatdb import attachments as att_mod
from imessage_chatdb import sql
from imessage_chatdb.attachments import (
    attachment_by_guid,
    attachments_for,
    chat_attachments,
    is_plugin_payload,
    payload_for,
)
from imessage_chatdb.connection import open_connection
from imessage_chatdb.models import Attachment
from tests.conftest import FixtureDB, MakeDB
from tests.fixtures import builders as b

PHONE = "+15550001234"
CHAT = "any;-;+15550001234"
OTHER_CHAT = "any;-;+15550009876"
PAYLOAD_NAME = "5A1B.pluginPayloadAttachment"


@pytest.fixture
def reader(fixture_db: FixtureDB) -> Iterator[sqlite3.Connection]:
    conn = open_connection(fixture_db.path)
    try:
        yield conn
    finally:
        conn.close()


def _trace(conn: sqlite3.Connection) -> list[str]:
    stmts: list[str] = []
    conn.set_trace_callback(stmts.append)
    return stmts


# ---------------------------------------------------------------------------
# is_plugin_payload
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (PAYLOAD_NAME, True),
        (".pluginPayloadAttachment", True),
        ("x.pluginPayloadAttachment.png", False),
        ("photo.png", False),
        ("", False),
        (None, False),
    ],
)
def test_is_plugin_payload(name: str | None, expected: bool) -> None:
    assert is_plugin_payload(name) is expected


# ---------------------------------------------------------------------------
# attachments_for
# ---------------------------------------------------------------------------


def _seed(w: sqlite3.Connection) -> tuple[int, int]:
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    msg = b.add_message(w, chat, text=None, has_att=1, handle=PHONE)
    b.add_attachment(w, msg, "ATT-PNG", "image/png", "photo.png", "~/synthetic/photo.png",
                     uti="public.png", total_bytes=1234)
    b.add_attachment(w, msg, "ATT-PAYLOAD", None, PAYLOAD_NAME,
                     "~/synthetic/" + PAYLOAD_NAME, hide=1)
    # hidden by Messages but NOT a plugin payload: the relay surfaces these
    b.add_attachment(w, msg, "ATT-HIDDEN-PNG", "image/png", "hidden.png",
                     "~/synthetic/hidden.png", hide=1)
    b.add_attachment(w, msg, "ATT-STICKER", "image/heic", "sticker.heic", None, sticker=1)
    return chat, msg


def test_default_excludes_only_plugin_payloads(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    _, msg = _seed(fixture_db.writer)
    got = attachments_for(reader, [msg])
    assert set(got) == {msg}
    guids = [a.guid for a in got[msg]]
    assert "ATT-PAYLOAD" not in guids
    assert set(guids) == {"ATT-PNG", "ATT-HIDDEN-PNG", "ATT-STICKER"}
    hidden = next(a for a in got[msg] if a.guid == "ATT-HIDDEN-PNG")
    assert hidden.hide_attachment is True and hidden.is_plugin_payload is False


def test_include_plugin_payloads_override(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    _, msg = _seed(fixture_db.writer)
    got = attachments_for(reader, [msg], include_plugin_payloads=True)
    guids = {a.guid for a in got[msg]}
    assert guids == {"ATT-PNG", "ATT-PAYLOAD", "ATT-HIDDEN-PNG", "ATT-STICKER"}
    payload = next(a for a in got[msg] if a.guid == "ATT-PAYLOAD")
    assert payload.is_plugin_payload is True and payload.mime_type is None


def test_attachment_fields(fixture_db: FixtureDB, reader: sqlite3.Connection) -> None:
    _, msg = _seed(fixture_db.writer)
    got = attachments_for(reader, [msg])
    by_guid = {a.guid: a for a in got[msg]}
    png = by_guid["ATT-PNG"]
    assert isinstance(png, Attachment)
    assert png.message_rowid == msg
    assert isinstance(png.rowid, int) and png.rowid > 0
    assert png.mime_type == "image/png"
    assert png.transfer_name == "photo.png"
    assert png.filename == "~/synthetic/photo.png"
    assert png.uti == "public.png"
    assert png.total_bytes == 1234
    assert png.is_sticker is False
    assert png.hide_attachment is False
    assert png.message_date is None
    sticker = by_guid["ATT-STICKER"]
    assert sticker.is_sticker is True and sticker.filename is None and sticker.path is None
    assert png.to_dict() == {
        "guid": "ATT-PNG", "mime_type": "image/png", "name": "photo.png",
        "filename": "~/synthetic/photo.png",
    }
    rowids = {a.rowid for a in got[msg]}
    assert len(rowids) == len(got[msg])  # distinct attachment ROWIDs


def test_attachments_for_keys_only_messages_that_have_rows(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    chat, msg = _seed(w)
    bare = b.add_message(w, chat, text="no attachments")
    payload_only = b.add_message(w, chat, text=None, has_att=1)
    b.add_attachment(w, payload_only, "ATT-P2", None, PAYLOAD_NAME, None)
    got = attachments_for(reader, [msg, bare, payload_only, 999_999, msg])
    assert set(got) == {msg}  # bare and payload-only rows have no key
    assert len(got[msg]) == 3
    with_payloads = attachments_for(reader, [payload_only], include_plugin_payloads=True)
    assert [a.guid for a in with_payloads[payload_only]] == ["ATT-P2"]


def test_attachments_for_is_one_query_and_none_when_empty(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    chat, msg = _seed(w)
    second = b.add_message(w, chat, text=None, has_att=1)
    b.add_attachment(w, second, "ATT-2", "video/mp4", "clip.mp4", "~/synthetic/clip.mp4")
    stmts = _trace(reader)
    got = attachments_for(reader, [msg, second])
    assert len(stmts) == 1 and "message_attachment_join" in stmts[0]
    assert set(got) == {msg, second}
    del stmts[:]
    assert attachments_for(reader, []) == {}
    assert attachments_for(reader, iter(())) == {}
    assert stmts == []


# ---------------------------------------------------------------------------
# Attachment.path / exists()
# ---------------------------------------------------------------------------


def test_path_expands_tilde_and_is_none_for_null_or_empty(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    msg = b.add_message(w, chat, text=None, has_att=1)
    b.add_attachment(w, msg, "A-TILDE", "image/png", "t.png", "~/synthetic/t.png")
    b.add_attachment(w, msg, "A-ABS", "image/png", "a.png", "/var/synthetic/a.png")
    b.add_attachment(w, msg, "A-NULL", "image/png", "n.png", None)
    b.add_attachment(w, msg, "A-EMPTY", "image/png", "e.png", "")
    by_guid = {a.guid: a for a in attachments_for(reader, [msg])[msg]}
    assert by_guid["A-TILDE"].path == Path.home() / "synthetic" / "t.png"
    assert by_guid["A-TILDE"].path is not None and by_guid["A-TILDE"].path.is_absolute()
    assert by_guid["A-ABS"].path == Path("/var/synthetic/a.png")
    assert by_guid["A-NULL"].path is None and by_guid["A-NULL"].filename is None
    assert by_guid["A-EMPTY"].path is None and by_guid["A-EMPTY"].filename == ""
    assert by_guid["A-NULL"].exists() is False
    assert by_guid["A-EMPTY"].exists() is False


def test_exists_checks_the_filesystem(
    fixture_db: FixtureDB, reader: sqlite3.Connection, tmp_path: Path
) -> None:
    w = fixture_db.writer
    real = tmp_path / "attachments" / "real.png"
    real.parent.mkdir()
    real.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 16)
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    msg = b.add_message(w, chat, text=None, has_att=1)
    b.add_attachment(w, msg, "A-REAL", "image/png", "real.png", str(real))
    b.add_attachment(w, msg, "A-GONE", "image/png", "gone.png", str(tmp_path / "gone.png"))
    by_guid = {a.guid: a for a in attachments_for(reader, [msg])[msg]}
    assert by_guid["A-REAL"].exists() is True and by_guid["A-REAL"].path == real
    assert by_guid["A-GONE"].exists() is False


# ---------------------------------------------------------------------------
# chat_attachments
# ---------------------------------------------------------------------------


def test_chat_attachments_ordered_by_message_date_desc(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    other = b.add_chat(w, OTHER_CHAT, handles=["+15550009876"])
    base = b.BASE_DATE_NS
    # ROWID order deliberately differs from date order
    m_mid = b.add_message(w, chat, text=None, has_att=1, date_ns=base + 20)
    m_new = b.add_message(w, chat, text=None, has_att=1, date_ns=base + 30)
    m_old = b.add_message(w, chat, text=None, has_att=1, date_ns=base + 10)
    m_other = b.add_message(w, other, text=None, has_att=1, date_ns=base + 40)
    b.add_attachment(w, m_mid, "A-MID", "image/png", "mid.png", "~/synthetic/mid.png")
    b.add_attachment(w, m_new, "A-NEW", "video/mp4", "new.mp4", "~/synthetic/new.mp4")
    b.add_attachment(w, m_new, "A-NEW-PAYLOAD", None, PAYLOAD_NAME, None)
    b.add_attachment(w, m_old, "A-OLD", "image/heic", "old.heic", "~/synthetic/old.heic")
    b.add_attachment(w, m_other, "A-OTHER", "image/png", "other.png", "~/synthetic/other.png")

    got = chat_attachments(reader, CHAT)
    assert [a.guid for a in got] == ["A-NEW", "A-MID", "A-OLD"]
    assert [a.message_rowid for a in got] == [m_new, m_mid, m_old]
    assert [a.message_date for a in got] == [base + 30, base + 20, base + 10]
    assert got[0].message_date_unix is not None
    assert all(isinstance(a, Attachment) for a in got)

    with_payload = chat_attachments(reader, CHAT, include_plugin_payloads=True)
    assert [a.guid for a in with_payload] == ["A-NEW", "A-NEW-PAYLOAD", "A-MID", "A-OLD"]
    assert chat_attachments(reader, "any;-;nobody@example.invalid") == []
    assert [a.guid for a in chat_attachments(reader, OTHER_CHAT)] == ["A-OTHER"]


def test_chat_attachments_ignores_cache_has_attachments_flag(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    """The verbatim CHAT_ATTACHMENTS statement joins the attachment tables directly."""
    w = fixture_db.writer
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    msg = b.add_message(w, chat, text="flag not set", has_att=0)
    b.add_attachment(w, msg, "A-FLAGLESS", "image/png", "f.png", "~/synthetic/f.png")
    assert [a.guid for a in chat_attachments(reader, CHAT)] == ["A-FLAGLESS"]


# ---------------------------------------------------------------------------
# attachment_by_guid / payload_for
# ---------------------------------------------------------------------------


def test_attachment_by_guid(fixture_db: FixtureDB, reader: sqlite3.Connection) -> None:
    _, msg = _seed(fixture_db.writer)
    a = attachment_by_guid(reader, "ATT-PNG")
    assert a is not None
    assert a.guid == "ATT-PNG" and a.message_rowid == msg
    assert a.filename == "~/synthetic/photo.png" and a.mime_type == "image/png"
    assert a.transfer_name == "photo.png" and a.message_date is None
    # no filter by guid: a caller naming the payload row gets it
    p = attachment_by_guid(reader, "ATT-PAYLOAD")
    assert p is not None and p.is_plugin_payload is True
    assert attachment_by_guid(reader, "NO-SUCH-GUID") is None


def test_attachment_by_guid_without_join_row(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    w.execute(
        "INSERT INTO attachment (guid, mime_type, transfer_name, filename) VALUES (?,?,?,?)",
        ("ORPHAN-ATT", "image/png", "o.png", None),
    )
    a = attachment_by_guid(reader, "ORPHAN-ATT")
    assert a is not None and a.message_rowid == 0 and a.path is None


def test_payload_for(fixture_db: FixtureDB, reader: sqlite3.Connection) -> None:
    w = fixture_db.writer
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    blob = b"bplist00" + bytes(range(32))
    with_payload = b.add_message(w, chat, text="link", payload=blob)
    without = b.add_message(w, chat, text="plain", payload=None)
    got = payload_for(reader, with_payload)
    assert got == blob and isinstance(got, bytes)
    assert payload_for(reader, without) is None
    assert payload_for(reader, 999_999) is None


def test_payload_for_without_payload_column(make_db: MakeDB) -> None:
    fx = make_db("macos14")
    w = fx.writer
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    rid = b.add_message(w, chat, text="x")
    w.execute("ALTER TABLE message DROP COLUMN payload_data")
    conn = open_connection(fx.path)
    try:
        assert payload_for(conn, rid) is None
    finally:
        conn.close()


def test_optional_attachment_columns_render_none_when_dropped(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    w = fx.writer
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    msg = b.add_message(w, chat, text=None, has_att=1)
    b.add_attachment(w, msg, "A-1", "image/png", "a.png", "~/synthetic/a.png")
    for col in ("uti", "total_bytes", "is_sticker", "hide_attachment"):
        w.execute(f"ALTER TABLE attachment DROP COLUMN {col}")
    conn = open_connection(fx.path)
    try:
        a = attachments_for(conn, [msg])[msg][0]
        assert a.uti is None and a.total_bytes is None
        assert a.is_sticker is None and a.hide_attachment is None
        assert a.guid == "A-1" and a.is_plugin_payload is False
        assert chat_attachments(conn, CHAT)[0].hide_attachment is None
        by_guid = attachment_by_guid(conn, "A-1")
        assert by_guid is not None and by_guid.is_sticker is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# statement shape: the relay's statements verbatim, extra result columns only
# ---------------------------------------------------------------------------


def _from_clause(statement: str) -> str:
    return statement[statement.index("FROM") :]


def _split_at_from_line(statement: str) -> tuple[str, str]:
    """``(select list, "\\n   FROM ..." remainder)`` of a relay statement."""
    m = att_mod._FROM_CLAUSE.search(statement)
    assert m is not None
    return statement[: m.start()], statement[m.start() :]


def test_wide_statements_are_the_verbatim_constants_plus_extra_columns() -> None:
    """The executed text is ``sql.X`` with ``, <extras>`` appended to its select list."""
    for wide, verbatim, extra in (
        (att_mod._ATTACHMENTS_FOR_WIDE, sql.ATTACHMENTS_FOR, att_mod.WIDE_EXTRA_COLUMNS),
        (
            att_mod._CHAT_ATTACHMENTS_WIDE,
            sql.CHAT_ATTACHMENTS,
            "m.ROWID AS mid, " + att_mod.WIDE_EXTRA_COLUMNS,
        ),
    ):
        # everything from FROM onward is byte-equal (joins, WHERE, ORDER BY)
        assert _from_clause(wide) == _from_clause(verbatim)
        # the relay's select list comes first, unchanged, then ", extras"; nothing else differs
        head, tail = _split_at_from_line(verbatim)
        assert wide == head + ", " + extra + tail
        assert wide.startswith(head + ", ")
    assert "ORDER BY" not in att_mod._ATTACHMENTS_FOR_WIDE
    assert att_mod._CHAT_ATTACHMENTS_WIDE.strip().endswith("ORDER BY m.date DESC")


def test_widen_rejects_a_statement_without_a_from_line() -> None:
    with pytest.raises(ValueError):
        att_mod._widen("SELECT 1")


def _plan(conn: sqlite3.Connection, statement: str, params: tuple[object, ...]) -> list[str]:
    rows = conn.execute("EXPLAIN QUERY PLAN " + statement, params).fetchall()
    return [str(r["detail"]) for r in rows]


def test_wide_statements_have_the_relays_query_plan_on_every_profile(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    """Row order where the relay has no ORDER BY is scan order; the planner
    must pick the same plan for the verbatim and the widened text."""
    w = fixture_db.writer
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    msgs = [b.add_message(w, chat, text=None, has_att=1) for _ in range(3)]
    for i, m in enumerate(msgs):
        b.add_attachment(w, m, f"A-{i}", "image/png", f"{i}.png", f"~/synthetic/{i}.png")
    w.execute("ANALYZE").fetchall()
    ids = tuple(msgs)
    assert _plan(reader, sql.expand_in(sql.ATTACHMENTS_FOR, len(ids)), ids) == _plan(
        reader, sql.expand_in(att_mod._ATTACHMENTS_FOR_WIDE, len(ids)), ids
    )
    assert _plan(reader, sql.CHAT_ATTACHMENTS, (CHAT,)) == _plan(
        reader, att_mod._CHAT_ATTACHMENTS_WIDE, (CHAT,)
    )


def test_wide_statements_return_the_relays_rows_in_the_relays_order(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    """Differential: the relay's verbatim statement and the widened one yield
    the same (guid, mime, name) sequence, including same-date ties."""
    w = fixture_db.writer
    chat = b.add_chat(w, CHAT, handles=[PHONE])
    base = b.BASE_DATE_NS
    # out-of-order dates, duplicate dates (ties), several attachments per message
    dates = [base + 20, base + 10, base + 20, base + 30, base + 10]
    msgs = [b.add_message(w, chat, text=None, has_att=1, date_ns=d) for d in dates]
    n = 0
    for m in msgs:
        for _ in range(2):
            b.add_attachment(w, m, f"A-{n:02d}", "image/png", f"{n}.png", f"~/synthetic/{n}.png")
            n += 1
    # attachment rows joined to messages in a different order than inserted
    for m in reversed(msgs):
        b.add_attachment(w, m, f"A-{n:02d}", None, PAYLOAD_NAME, None)
        n += 1

    ids = tuple(reversed(msgs))
    relay_rows = reader.execute(sql.expand_in(sql.ATTACHMENTS_FOR, len(ids)), ids).fetchall()
    lib_rows = reader.execute(
        sql.expand_in(att_mod._ATTACHMENTS_FOR_WIDE, len(ids)), ids
    ).fetchall()
    key = ["mid", "guid", "mime", "name"]
    assert [[r[k] for k in key] for r in relay_rows] == [[r[k] for k in key] for r in lib_rows]
    # and the library's filtered result keeps that order per message
    got = attachments_for(reader, ids, include_plugin_payloads=True)
    flat = [a.guid for m in ids for a in got.get(m, [])]
    by_relay: dict[int, list[str]] = {}
    for r in relay_rows:
        by_relay.setdefault(int(r["mid"]), []).append(str(r["guid"]))
    assert flat == [g for m in ids for g in by_relay.get(m, [])]

    relay_rows = reader.execute(sql.CHAT_ATTACHMENTS, (CHAT,)).fetchall()
    lib_rows = reader.execute(att_mod._CHAT_ATTACHMENTS_WIDE, (CHAT,)).fetchall()
    key = ["guid", "mime", "name", "date"]
    assert [[r[k] for k in key] for r in relay_rows] == [[r[k] for k in key] for r in lib_rows]
    assert [a.guid for a in chat_attachments(reader, CHAT, include_plugin_payloads=True)] == [
        str(r["guid"]) for r in relay_rows
    ]
