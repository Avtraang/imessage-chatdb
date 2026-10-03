"""search.py: NUL-safe content search, snippet windows, URL extraction (DESIGN.md 6.5, 8.4)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from imessage_chatdb.connection import open_connection
from imessage_chatdb.models import SearchHit
from imessage_chatdb.search import extract_urls, search, snippet
from tests.conftest import FixtureDB
from tests.fixtures.builders import add_chat, add_message
from tests.fixtures.typedstream_writer import encode_attributed_body

A = "+15550001234"
B = "+15550005678"


@pytest.fixture
def reader(fixture_db: FixtureDB) -> Iterator[sqlite3.Connection]:
    conn = open_connection(fixture_db.path)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, []),
        ("", []),
        ("no links here", []),
        ("see https://example.invalid/a.", ["https://example.invalid/a"]),
        ("(http://example.invalid/b), ok", ["http://example.invalid/b"]),
        ("x https://e.invalid/c); y https://e.invalid/c", ["https://e.invalid/c", "https://e.invalid/c"]),
        ("https://e.invalid/1 then http://e.invalid/2", ["https://e.invalid/1", "http://e.invalid/2"]),
        ("ftp://e.invalid/no https://e.invalid/?q=1&r=2", ["https://e.invalid/?q=1&r=2"]),
        ("https://e.invalid/trail...,;)", ["https://e.invalid/trail"]),
    ],
)
def test_extract_urls(text: str | None, expected: list[str]) -> None:
    assert extract_urls(text) == expected


def _relay_snippet(text: str, i: int) -> str:
    start = max(0, i - 40)
    return (
        ("…" if start else "")
        + text[start : start + 120]
        + ("…" if len(text) > start + 120 else "")
    )


@pytest.mark.parametrize(
    ("text", "index"),
    [
        ("short", 0),
        ("short", 3),
        ("x" * 300, 0),
        ("x" * 300, 40),
        ("x" * 300, 41),
        ("x" * 300, 250),
        ("y" * 120, 0),
        ("y" * 121, 0),
        ("z" * 160, 41),
    ],
)
def test_snippet_matches_relay_expression(text: str, index: int) -> None:
    assert snippet(text, index) == _relay_snippet(text, index)


def test_snippet_windows_and_markers() -> None:
    text = "".join(chr(ord("a") + i % 26) for i in range(200))
    assert snippet(text, 10) == text[:120] + "…"  # start clamps at 0, no leading marker
    assert snippet(text, 40) == text[:120] + "…"
    assert snippet(text, 41) == "…" + text[1:121] + "…"
    assert snippet(text, 199) == "…" + text[159:]  # tail fits: no trailing marker
    assert snippet("tiny", 2) == "tiny"
    assert snippet(text, 100, before=10, width=20) == "…" + text[90:110] + "…"


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_empty_query_returns_nothing_without_sql(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    rid = add_chat(fixture_db.writer, f"any;-;{A}", 45)
    add_message(fixture_db.writer, rid, text="anything")
    statements: list[str] = []
    reader.set_trace_callback(statements.append)
    assert search(reader, "") == []
    assert search(reader, "   ") == []
    reader.set_trace_callback(None)
    assert statements == []


def test_text_column_and_blob_only_hits(fixture_db: FixtureDB, reader: sqlite3.Connection) -> None:
    w = fixture_db.writer
    rid = add_chat(w, "any;+;g", 43, display_name="Crew")
    m_text = add_message(w, rid, text="Meet at the pier tonight", handle=A)
    blob = encode_attributed_body("pier review done")
    assert b"\x00" in blob[: blob.index(b"pier")]  # a NUL precedes the text: LIKE cannot see it
    m_blob = add_message(w, rid, body=blob, is_from_me=1)
    add_message(w, rid, text="unrelated", handle=B)

    hits = search(reader, "pier")
    assert [h.rowid for h in hits] == [m_blob, m_text]  # ROWID DESC
    assert all(isinstance(h, SearchHit) for h in hits)
    h = hits[1]
    assert h.text == "Meet at the pier tonight" and h.match_index == 12
    assert h.chat_rowid == rid and h.chat_guid == "any;+;g" and h.chat_identifier == "g"
    assert h.chat_display_name == "Crew" and h.is_group is True and h.chat_name == "Crew"
    assert h.is_from_me is False and h.sender_handle == A
    assert isinstance(h.date, int) and h.date > 0
    assert hits[0].is_from_me is True and hits[0].sender_handle is None
    assert hits[0].text == "pier review done" and hits[0].match_index == 0
    assert isinstance(h.to_dict()["date"], float)


def test_case_variants(fixture_db: FixtureDB, reader: sqlite3.Connection) -> None:
    w = fixture_db.writer
    rid = add_chat(w, f"any;-;{A}", 45)
    m_upper_col = add_message(w, rid, text="HELLO there")  # LIKE is case-insensitive
    m_cap_blob = add_message(w, rid, body=encode_attributed_body("Hello blob"))
    m_upper_blob = add_message(w, rid, body=encode_attributed_body("say HELLO"))
    m_lower_blob = add_message(w, rid, body=encode_attributed_body("hello again"))
    m_exact_blob = add_message(w, rid, body=encode_attributed_body("hELLo odd"))

    assert [h.rowid for h in search(reader, "hello")] == [
        m_lower_blob,
        m_upper_blob,
        m_cap_blob,
        m_upper_col,
    ]
    # the query as typed is one of the four byte variants
    hits = search(reader, "hELLo")
    assert m_exact_blob in [h.rowid for h in hits]
    assert all(h.match_index == h.text.lower().find("hello") for h in hits)


def test_recheck_drops_attribute_key_false_positive(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    rid = add_chat(w, f"any;-;{A}", 45)
    blob = encode_attributed_body("nothing to see")
    assert b"NSDictionary" in blob and b"__kIMMessagePartAttributeName" in blob
    add_message(w, rid, body=blob)
    assert search(reader, "NSDictionary") == []
    assert search(reader, "kIMMessagePart") == []
    real = add_message(w, rid, body=encode_attributed_body("the NSDictionary class"))
    assert [h.rowid for h in search(reader, "NSDictionary")] == [real]


def test_tapbacks_are_excluded(fixture_db: FixtureDB, reader: sqlite3.Connection) -> None:
    w = fixture_db.writer
    rid = add_chat(w, f"any;-;{A}", 45)
    m = add_message(w, rid, text="pizza tonight?")
    add_message(w, rid, text="Loved “pizza tonight?”", assoc_type=2000, assoc_guid="p:0/G")
    add_message(w, rid, text="pizza sticker", assoc_type=1000, assoc_guid="p:0/G")
    add_message(w, rid, text="removed pizza", assoc_type=3000, assoc_guid="p:0/G")
    add_message(w, rid, body=encode_attributed_body("pizza emoji"), assoc_type=2006)
    assert [h.rowid for h in search(reader, "pizza")] == [m]


def test_limit_and_oversample(fixture_db: FixtureDB, reader: sqlite3.Connection) -> None:
    w = fixture_db.writer
    rid = add_chat(w, f"any;-;{A}", 45)
    rowids = [add_message(w, rid, text=f"apple {i}") for i in range(5)]
    assert [h.rowid for h in search(reader, "apple")] == rowids[::-1]
    assert [h.rowid for h in search(reader, "apple", limit=2)] == [rowids[4], rowids[3]]

    # Oversample: four newer blobs match "NSDictionary" only in their archive
    # bytes; the real hit is older.  limit*oversample = 4 never reaches it.
    real = add_message(w, rid, text="NSDictionary in text")
    for i in range(4):
        add_message(w, rid, body=encode_attributed_body(f"decoy {i}"))
    assert search(reader, "NSDictionary", limit=2, oversample=2) == []
    assert [h.rowid for h in search(reader, "NSDictionary", limit=2, oversample=3)] == [real]
    assert [h.rowid for h in search(reader, "NSDictionary", limit=1, oversample=5)] == [real]


def test_query_is_stripped_and_text_cleaned(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    rid = add_chat(w, f"any;-;{A}", 45)
    m = add_message(w, rid, text="￼  spaced   out\nwords ")
    (hit,) = search(reader, "  out  ")
    assert hit.rowid == m
    assert hit.text == "spaced out words" and hit.match_index == 7
    # The SQL side matches the raw column, so a query spanning collapsed
    # whitespace cannot hit (the relay's behaviour; the recheck sees cleaned text).
    assert search(reader, "out words") == []


def test_empty_text_column_falls_back_to_blob(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    rid = add_chat(w, f"any;-;{A}", 45)
    m = add_message(w, rid, text="", body=encode_attributed_body("fallback text"))
    (hit,) = search(reader, "fallback")
    assert hit.rowid == m and hit.text == "fallback text"


def test_orphan_messages_are_not_searched(
    fixture_db: FixtureDB, reader: sqlite3.Connection
) -> None:
    w = fixture_db.writer
    rid = add_chat(w, f"any;-;{A}", 45)
    add_message(w, rid, text="orphan needle", join=False)
    assert search(reader, "needle") == []
