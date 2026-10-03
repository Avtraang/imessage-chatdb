"""Pins for ``imessage_chatdb.typedstream`` (DESIGN.md sections 4.3, 6.2, 8.2).

Every blob is fabricated by ``tests.fixtures.typedstream_writer``; no real
database or message is involved.
"""

from __future__ import annotations

import pytest

from imessage_chatdb.typedstream import clean_text, effective_text, extract_text
from tests.fixtures.typedstream_writer import (
    MUTABLE_SKELETON,
    PLAIN_SKELETON,
    TRAILER_HEAD,
    TRAILER_TAIL,
    encode_attributed_body,
    encode_int,
)

HEADER_HEX = "040b73747265616d7479706564" + "81e803"


def _legacy_extract_text(data: bytes | None) -> str | None:
    """Verbatim copy of the relay's byte-scan, the legacy oracle.

    Note the 3-byte ``0x82`` read; the library deliberately reads 4.
    """
    if not data:
        return None
    try:
        start = data.index(b"NSString") + 6
        chunk = data[start:]
        p = chunk.find(b"\x2b")
        if p == -1:
            return None
        j = p + 1
        length = chunk[j]
        j += 1
        if length == 0x81:
            length = int.from_bytes(chunk[j : j + 2], "little")
            j += 2
        elif length == 0x82:
            length = int.from_bytes(chunk[j : j + 3], "little")
            j += 3
        return chunk[j : j + length].decode("utf-8", errors="replace") or None
    except Exception:
        return None


def _text_end(text: str, *, mutable: bool = False, force_len_tag: int | None = None) -> int:
    """Offset just past the last UTF-8 byte of the text inside the encoded blob."""
    skeleton = MUTABLE_SKELETON if mutable else PLAIN_SKELETON
    utf8 = text.encode("utf-8")
    return len(skeleton) + len(encode_int(len(utf8), force_tag=force_len_tag)) + len(utf8)


# ---------- writer: golden bytes ----------


@pytest.mark.parametrize("mutable", [False, True])
def test_writer_golden_header(mutable: bool) -> None:
    blob = encode_attributed_body("hi", mutable=mutable)
    assert blob.hex().startswith(HEADER_HEX)
    assert blob.startswith(b"\x04\x0bstreamtyped\x81\xe8\x03")


@pytest.mark.parametrize("mutable", [False, True])
def test_writer_golden_trailer(mutable: bool) -> None:
    text = "hello"
    blob = encode_attributed_body(text, mutable=mutable)
    end = _text_end(text, mutable=mutable)
    assert blob[end] == 0x86
    assert blob[end + 1 : end + 5] == bytes.fromhex("84026949")
    assert blob[end : end + 5] == TRAILER_HEAD
    # run length (5 UTF-16 units) + attribute count (1) + fixed tail
    assert blob[end + 5 :] == b"\x05" + b"\x01" + TRAILER_TAIL


def test_writer_skeletons_match_design() -> None:
    assert PLAIN_SKELETON == (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00"
        b"\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
    )
    assert MUTABLE_SKELETON == (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00"
        b"\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x0fNSMutableString\x01"
        b"\x84\x84\x08NSString\x01\x95\x84\x01+"
    )
    assert PLAIN_SKELETON.endswith(b"\x84\x01+")
    assert MUTABLE_SKELETON.endswith(b"\x84\x01+")
    # "NSString" occurs exactly once in each skeleton (not inside the class names).
    assert PLAIN_SKELETON.count(b"NSString") == 1
    assert MUTABLE_SKELETON.count(b"NSString") == 1


def test_writer_run_length_is_utf16_units() -> None:
    # U+1F600 is one code point, 4 UTF-8 bytes, 2 UTF-16 units.
    text = "a\U0001f600"
    blob = encode_attributed_body(text)
    end = _text_end(text)
    assert blob[len(PLAIN_SKELETON)] == 5  # LEN in bytes
    assert blob[end + 5] == 3  # run length in UTF-16 units


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, b"\x00"),
        (1, b"\x01"),
        (127, b"\x7f"),
        (128, b"\x81\x80\x00"),
        (65535, b"\x81\xff\xff"),
        (65536, b"\x82\x00\x00\x01\x00"),
        (70_000, b"\x82" + (70_000).to_bytes(4, "little")),
        (1 << 32, b"\x83" + (1 << 32).to_bytes(8, "little")),
    ],
)
def test_encode_int_natural(value: int, expected: bytes) -> None:
    assert encode_int(value) == expected


def test_encode_int_forced_and_errors() -> None:
    assert encode_int(5, force_tag=0x81) == b"\x81\x05\x00"
    assert encode_int(5, force_tag=0x82) == b"\x82\x05\x00\x00\x00"
    assert encode_int(5, force_tag=0x83) == b"\x83" + (5).to_bytes(8, "little")
    with pytest.raises(ValueError):
        encode_int(70_000, force_tag=0x81)
    with pytest.raises(ValueError):
        encode_int(5, force_tag=0x80)
    with pytest.raises(ValueError):
        encode_int(-1)


# ---------- extract_text: lengths ----------


@pytest.mark.parametrize("n", [1, 127, 128, 65535, 65536])
@pytest.mark.parametrize("mutable", [False, True])
def test_extract_text_pinned_lengths(n: int, mutable: bool) -> None:
    # Vary the last byte so a truncated or off-by-one read is detectable.
    text = ("x" * (n - 1) + "Z") if n > 1 else "Z"
    assert len(text.encode("utf-8")) == n
    blob = encode_attributed_body(text, mutable=mutable)
    out = extract_text(blob)
    assert out == text
    assert out is not None
    assert len(out) == n
    assert out[-1] == "Z"
    assert not out.startswith("\x00")


def test_extract_text_length_zero_is_none() -> None:
    blob = encode_attributed_body("")
    assert blob[len(PLAIN_SKELETON)] == 0
    assert extract_text(blob) is None


def test_extract_text_70000_bytes_forces_0x82() -> None:
    text = "x" * 69_999 + "Z"
    blob = encode_attributed_body(text)
    off = len(PLAIN_SKELETON)
    assert blob[off] == 0x82
    assert blob[off + 1 : off + 5] == (70_000).to_bytes(4, "little")
    out = extract_text(blob)
    assert out == text
    assert out is not None
    assert len(out) == 70_000
    assert out[-1] == "Z"
    assert not out.startswith("\x00")
    assert "\x00" not in out


def test_extract_text_0x82_differs_from_relay_three_byte_read() -> None:
    """The one knowing deviation (DESIGN.md 6.2): the relay misreads a 0x82 length."""
    text = "x" * 69_999 + "Z"
    blob = encode_attributed_body(text)
    legacy = _legacy_extract_text(blob)
    assert extract_text(blob) == text
    # The relay consumes only 3 of the 4 length bytes, so its text starts with
    # the fourth (0x00) byte and is shifted by one.
    assert legacy is not None
    assert legacy != text
    assert legacy.startswith("\x00")


@pytest.mark.parametrize("tag", [0x81, 0x82, 0x83])
@pytest.mark.parametrize("mutable", [False, True])
def test_extract_text_forced_wide_tags(tag: int, mutable: bool) -> None:
    text = "short"
    blob = encode_attributed_body(text, mutable=mutable, force_len_tag=tag)
    skeleton = MUTABLE_SKELETON if mutable else PLAIN_SKELETON
    assert blob[len(skeleton)] == tag
    assert extract_text(blob) == text


def test_extract_text_forced_0x83_large_value() -> None:
    text = "y" * 300
    blob = encode_attributed_body(text, force_len_tag=0x83)
    assert blob[len(PLAIN_SKELETON)] == 0x83
    assert extract_text(blob) == text


# ---------- extract_text: encodings ----------


@pytest.mark.parametrize(
    "text",
    [
        "\U0001f600\U0001f44d",  # emoji: 4 bytes each, 1 code point each
        "שלום",  # Hebrew: 2 bytes each
        "café — שלום \U0001f600",
        "line1\nline2\ttabbed",
        "￼",  # attachment placeholder survives extract_text; clean_text drops it
        "+ plus + inside + text +",
        "NSString NSAttributedString in the text itself",
    ],
)
def test_extract_text_unicode_roundtrip(text: str) -> None:
    utf8_len = len(text.encode("utf-8"))
    blob = encode_attributed_body(text)
    assert utf8_len < 0x80
    assert blob[len(PLAIN_SKELETON)] == utf8_len
    assert extract_text(blob) == text
    assert extract_text(encode_attributed_body(text, mutable=True)) == text


def test_extract_text_length_is_bytes_not_code_points() -> None:
    text = "שלום"  # 4 code points, 8 bytes
    blob = encode_attributed_body(text)
    assert blob[len(PLAIN_SKELETON)] == 8
    assert extract_text(blob) == text
    # If the writer wrote code points instead of bytes, only half would decode.
    bad = PLAIN_SKELETON + b"\x04" + text.encode("utf-8") + TRAILER_HEAD
    assert extract_text(bad) == "של"


def test_extract_text_text_length_43_tag_equals_plus() -> None:
    # LEN byte 0x2b == "+" must not be mistaken for the type marker.
    text = "a" * 43
    blob = encode_attributed_body(text)
    assert blob[len(PLAIN_SKELETON)] == 0x2B
    assert extract_text(blob) == text


def test_extract_text_invalid_utf8_replaced() -> None:
    raw = b"ok\xff\xfe\xc3"
    blob = PLAIN_SKELETON + bytes([len(raw)]) + raw + TRAILER_HEAD + b"\x05\x01" + TRAILER_TAIL
    out = extract_text(blob)
    assert out is not None
    assert out.startswith("ok")
    assert "�" in out
    assert len(out) == 5


# ---------- extract_text: None cases, never raises ----------


@pytest.mark.parametrize("data", [None, b"", bytearray()])
def test_extract_text_empty_inputs(data: bytes | None) -> None:
    assert extract_text(data) is None


def test_extract_text_no_nsstring() -> None:
    blob = encode_attributed_body("hello").replace(b"NSString", b"NSStrong")
    assert extract_text(blob) is None
    assert extract_text(b"\x04\x0bstreamtyped\x81\xe8\x03random bytes") is None
    assert extract_text(b"\x00" * 64) is None


def test_extract_text_no_plus_after_nsstring() -> None:
    blob = encode_attributed_body("hello")
    cut = blob[: blob.index(b"+")]
    assert extract_text(cut) is None
    assert extract_text(cut.replace(b"NSString\x01", b"NSString\x00")) is None


@pytest.mark.parametrize("tag", [0x80, 0x84, 0x85, 0x86, 0x90, 0x92, 0xFF])
def test_extract_text_unknown_tag_is_none(tag: int) -> None:
    text = "hello"
    body = text.encode("utf-8")
    blob = PLAIN_SKELETON + bytes([tag]) + body + TRAILER_HEAD + b"\x05\x01" + TRAILER_TAIL
    assert extract_text(blob) is None
    # The relay would have treated 0x80/0x90 as a raw length; the library refuses.
    assert _legacy_extract_text(blob) is not None


@pytest.mark.parametrize("mutable", [False, True])
@pytest.mark.parametrize("force_len_tag", [None, 0x81, 0x82, 0x83])
def test_extract_text_every_truncation_prefix(mutable: bool, force_len_tag: int | None) -> None:
    text = "truncate me ש\U0001f600"
    blob = encode_attributed_body(text, mutable=mutable, force_len_tag=force_len_tag)
    end = _text_end(text, mutable=mutable, force_len_tag=force_len_tag)
    for cut in range(len(blob) + 1):
        prefix = blob[:cut]
        out = extract_text(prefix)  # must never raise
        if cut < end:
            # Header, length tag or text cut short -> None, never a partial string.
            assert out is None, (cut, out)
        else:
            # The whole text is present; the trailer is not needed.
            assert out == text, cut


def test_extract_text_truncation_prefixes_of_70000_byte_blob() -> None:
    text = "x" * 69_999 + "Z"
    blob = encode_attributed_body(text)
    end = _text_end(text)
    tag_off = len(PLAIN_SKELETON)
    sampled = [
        *range(0, tag_off + 6),  # every header / tag-byte cut
        tag_off + 100,
        end // 2,
        end - 1,
        end,
        end + 1,
        len(blob) - 1,
        len(blob),
    ]
    for cut in sampled:
        out = extract_text(blob[:cut])
        if cut < end:
            assert out is None, cut
        else:
            assert out == text, cut


def test_extract_text_garbage_never_raises() -> None:
    import random

    rng = random.Random(1234)
    base = encode_attributed_body("fuzz seed \U0001f600")
    for _ in range(500):
        data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 200)))
        out = extract_text(data)
        assert out is None or isinstance(out, str)
        flipped = bytearray(base)
        for _ in range(rng.randrange(1, 6)):
            flipped[rng.randrange(len(flipped))] ^= 1 << rng.randrange(8)
        out = extract_text(bytes(flipped))
        assert out is None or isinstance(out, str)


# ---------- extract_text == relay on every non-0x82 blob ----------


@pytest.mark.parametrize("mutable", [False, True])
@pytest.mark.parametrize(
    "text",
    ["a", "hello world", "x" * 127, "x" * 128, "y" * 65535, "\U0001f600" * 20, "ש" * 3000],
)
def test_extract_text_matches_relay_on_live_shapes(text: str, mutable: bool) -> None:
    blob = encode_attributed_body(text, mutable=mutable)
    assert extract_text(blob) == _legacy_extract_text(blob) == text


# ---------- effective_text: the four combinations ----------


def test_effective_text_text_column_wins() -> None:
    blob = encode_attributed_body("from blob")
    assert effective_text("from column", blob) == "from column"
    assert effective_text("from column", None) == "from column"
    assert effective_text("from column", b"") == "from column"


def test_effective_text_none_column_with_blob() -> None:
    blob = encode_attributed_body("from blob")
    assert effective_text(None, blob) == "from blob"


def test_effective_text_empty_column_with_blob() -> None:
    blob = encode_attributed_body("from blob")
    assert effective_text("", blob) == "from blob"


def test_effective_text_none_column_no_blob_stays_none() -> None:
    assert effective_text(None, None) is None
    assert effective_text(None, b"") is None


def test_effective_text_empty_column_no_blob_stays_empty() -> None:
    assert effective_text("", None) == ""
    assert effective_text("", b"") == ""


def test_effective_text_empty_column_with_failing_blob_is_none() -> None:
    """'' + undecodable blob -> None (the relay's result), not ''."""
    assert effective_text("", b"not a typedstream") is None
    assert effective_text("", encode_attributed_body("")) is None
    assert effective_text(None, b"not a typedstream") is None


def test_effective_text_matches_relay_rule() -> None:
    """Replays the relay's text rule on a grid of inputs."""
    blobs: list[bytes | None] = [
        None,
        b"",
        b"garbage",
        encode_attributed_body(""),
        encode_attributed_body("blob text"),
    ]
    columns: list[str | None] = [None, "", "column text"]
    for col in columns:
        for blob in blobs:
            legacy = col
            if not legacy and blob:
                legacy = _legacy_extract_text(blob)
            assert effective_text(col, blob) == legacy, (col, blob)


# ---------- clean_text ----------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("", ""),
        ("￼", ""),
        ("￼  \n\t", ""),
        ("  hello   world  ", "hello world"),
        ("a￼b", "ab"),
        ("￼ photo ￼ caption\n\nline", "photo caption line"),
        ("שלום \U0001f600", "שלום \U0001f600"),
        ("keep � replacement", "keep � replacement"),
    ],
)
def test_clean_text(value: str | None, expected: str) -> None:
    assert clean_text(value) == expected


def test_clean_text_is_relay_expression() -> None:
    for s in ["  a ￼ b\n", "￼", "x", "  tabs\t\there  "]:
        assert clean_text(s) == " ".join(s.replace("￼", "").split())


def test_clean_text_of_extracted_placeholder_is_empty() -> None:
    blob = encode_attributed_body("￼")
    assert extract_text(blob) == "￼"
    assert clean_text(effective_text(None, blob)) == ""
