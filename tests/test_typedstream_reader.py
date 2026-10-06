"""Tests for ``imessage_chatdb.typedstream_reader`` (docs/TYPEDSTREAM.md section 9.6).

Every blob here is fabricated by ``tests.fixtures.typedstream_writer`` or
hand-built from the grammar in docs/TYPEDSTREAM.md; no real database or
message is involved.  ``extract_text`` (0.1, untouched) is the oracle for the
text of every well-formed blob.
"""

from __future__ import annotations

import dataclasses
import io
import json
import random
# Wall-clock bounds in this module exist to catch hangs and quadratic blow-ups
# (which take minutes), not to benchmark: they are set an order of magnitude
# above what a laptop needs so shared CI runners never trip them.
import time
from collections.abc import Mapping
from typing import Any

import pytest

import imessage_chatdb
from imessage_chatdb.__main__ import EXIT_OK, build_parser, main
from imessage_chatdb.models import Message
from imessage_chatdb.typedstream import extract_text
from imessage_chatdb.typedstream_reader import (
    AttachmentPart,
    AttributedBody,
    AttributeRun,
    Mention,
    TextPart,
    TypedStreamError,
    UnknownPart,
    UnknownValue,
    Url,
    message_parts,
    parse_attributed_body,
    try_parse_attributed_body,
)
from tests.conftest import MakeDB
from tests.fixtures import builders as b
from tests.fixtures.typedstream_writer import (
    _CHAIN_ATTRIBUTED,
    _CHAIN_DICTIONARY,
    DATA_DETECTED,
    FILE_TRANSFER_GUID,
    LINK,
    LINK_IS_RICH,
    MENTION,
    MUTABLE_ROOT_SKELETON,
    ONE_TIME_CODE,
    PART,
    PLAIN_SKELETON,
    URL,
    WRITING_DIRECTION,
    Data,
    Number,
    _Archiver,
    encode_attributed_body,
    encode_attributed_body_runs,
    encode_int,
    encode_sint,
)

HEADER = b"\x04\x0bstreamtyped\x81\xe8\x03"
BREADCRUMB_MARKER = "__kIMBreadcrumbTextMarkerAttributeName"
BREADCRUMB_FLAGS = "__kIMBreadcrumbTextOptionFlags"
RICH_CARDS = "__kIMRichCardsAttributeName"

THREE_PARTS_TEXT = "￼ see @Sam https://example.test/x"
THREE_PARTS_RUNS: list[tuple[int, dict[str, object]]] = [
    (1, {FILE_TRANSFER_GUID: "AT_0_0000-FAKE-GUID", WRITING_DIRECTION: Number(-1, "q"), PART: 0}),
    (5, {PART: 1}),
    (4, {PART: 1, MENTION: "+15550100"}),
    (1, {PART: 1}),
    (22, {LINK: URL("https://example.test/x"), PART: 1, LINK_IS_RICH: True}),
]

PHONE = "+15550001234"
CHAT = "any;-;+15550001234"


def golden_hi(*, mutable: bool = False) -> bytes:
    return encode_attributed_body_runs("hi", [(2, {PART: 0})], mutable=mutable)


def golden_three_parts(**flags: bool) -> bytes:
    return encode_attributed_body_runs(THREE_PARTS_TEXT, THREE_PARTS_RUNS, mutable=True, **flags)


def golden_data() -> bytes:
    runs: list[tuple[int, dict[str, object]]] = [
        (1, {PART: 0, DATA_DETECTED: b"\x00\x01\x02"}),
        (1, {PART: 0, ONE_TIME_CODE: {"code": "1234"}}),
    ]
    return encode_attributed_body_runs("ab", runs)


def plain(value: object) -> object:
    """Mappings (``MappingProxyType``) as plain dicts, recursively, for comparisons."""
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    return value


def replace_once(blob: bytes, old: bytes, new: bytes) -> bytes:
    assert blob.count(old) == 1, (old, blob.count(old))
    return blob.replace(old, new)


def raises(blob: bytes, fragment: str) -> None:
    """``parse_attributed_body`` raises naming ``fragment``; ``try_parse`` returns ``None``."""
    with pytest.raises(TypedStreamError) as info:
        parse_attributed_body(blob)
    assert fragment in str(info.value), str(info.value)
    assert try_parse_attributed_body(blob) is None


# ---------------------------------------------------------------------------
# golden blobs (docs/TYPEDSTREAM.md section 8.1)
# ---------------------------------------------------------------------------


def test_golden_plain_hi() -> None:
    body = parse_attributed_body(golden_hi())
    assert body.text == "hi"
    assert body.root_class == "NSAttributedString"
    assert body.mutable is False
    assert body.runs == (AttributeRun(0, 2, {PART: 0}),)
    assert body.runs[0].length == 2
    assert body.runs[0].chars(body.text) == "hi"
    assert plain(body.runs[0].attributes) == {PART: 0}


def test_golden_mutable_hi() -> None:
    blob = golden_hi(mutable=True)
    assert blob.startswith(MUTABLE_ROOT_SKELETON)
    body = parse_attributed_body(blob)
    assert body.text == "hi"
    assert body.root_class == "NSMutableAttributedString"
    assert body.mutable is True
    assert body.runs == parse_attributed_body(golden_hi()).runs


def test_golden_three_parts_runs_and_values() -> None:
    body = parse_attributed_body(golden_three_parts())
    assert body.text == THREE_PARTS_TEXT
    assert body.mutable
    assert [(r.start, r.end) for r in body.runs] == [(0, 1), (1, 6), (6, 10), (10, 11), (11, 33)]
    assert body.runs[-1].end == len(THREE_PARTS_TEXT.encode("utf-16-le")) // 2
    assert [r.chars(body.text) for r in body.runs] == ["￼", " see ", "@Sam", " ", "https://example.test/x"]
    assert plain(body.runs[0].attributes) == {
        FILE_TRANSFER_GUID: "AT_0_0000-FAKE-GUID",
        WRITING_DIRECTION: -1,  # Number(-1, "q")
        PART: 0,
    }
    assert plain(body.runs[1].attributes) == {PART: 1}
    assert plain(body.runs[2].attributes) == {PART: 1, MENTION: "+15550100"}
    assert plain(body.runs[4].attributes) == {
        LINK: Url("https://example.test/x"),
        PART: 1,
        LINK_IS_RICH: 1,  # True -> NSNumber 'c' = 1
    }
    # runs 2 and 4 of the archive are back-references to dictionary 2: one mapping object
    assert body.runs[1].attributes is body.runs[3].attributes


def test_golden_data_and_nested_dictionary() -> None:
    body = parse_attributed_body(golden_data())
    assert body.text == "ab"
    assert plain(body.runs[0].attributes) == {PART: 0, DATA_DETECTED: b"\x00\x01\x02"}
    assert plain(body.runs[1].attributes) == {PART: 0, ONE_TIME_CODE: {"code": "1234"}}
    nested = body.runs[1].attributes[ONE_TIME_CODE]
    assert isinstance(nested, Mapping) and not isinstance(nested, dict)
    assert nested == {"code": "1234"}


def test_mutable_data_and_explicit_numbers() -> None:
    bold = "__kIMTextBoldAttributeName"
    runs: list[tuple[int, dict[str, object]]] = [
        (1, {PART: Number(3, "q"), bold: Number(1, "c"), "x": Data(b"\xff", mutable=True)})
    ]
    body = parse_attributed_body(encode_attributed_body_runs("a", runs))
    assert plain(body.runs[0].attributes) == {PART: 3, bold: 1, "x": b"\xff"}
    assert b"NSMutableData" in encode_attributed_body_runs("a", runs)


def test_results_are_frozen_and_attributes_read_only() -> None:
    body = parse_attributed_body(golden_hi())
    with pytest.raises(dataclasses.FrozenInstanceError):
        body.text = "no"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        body.runs[0].start = 5  # type: ignore[misc]
    with pytest.raises(TypeError):
        body.runs[0].attributes["x"] = 1  # type: ignore[index]


def test_bytearray_and_memoryview_input() -> None:
    blob = golden_hi()
    assert parse_attributed_body(bytearray(blob)) == parse_attributed_body(blob)
    assert parse_attributed_body(memoryview(blob)) == parse_attributed_body(blob)  # type: ignore[arg-type]
    assert try_parse_attributed_body(bytearray(blob)) == parse_attributed_body(blob)


# ---------------------------------------------------------------------------
# the reference mechanism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("share_keys", [False, True])
@pytest.mark.parametrize("share_values", [False, True])
def test_share_flags_parse_to_the_same_body(share_keys: bool, share_values: bool) -> None:
    blob = golden_three_parts(share_keys=share_keys, share_values=share_values)
    assert parse_attributed_body(blob) == parse_attributed_body(golden_three_parts())
    if share_keys:
        assert b"\x92\xa2" in blob  # type "@" + reference to the first key object
        assert blob.count(b"__kIMMessagePartAttributeName") == 1


def test_plain_and_mutable_roots_differ_only_in_root_class() -> None:
    plain_body = parse_attributed_body(
        encode_attributed_body_runs(THREE_PARTS_TEXT, THREE_PARTS_RUNS)
    )
    mutable_body = parse_attributed_body(golden_three_parts())
    assert plain_body.runs == mutable_body.runs
    assert plain_body.text == mutable_body.text
    assert (plain_body.root_class, mutable_body.root_class) == (
        "NSAttributedString",
        "NSMutableAttributedString",
    )


def test_shared_url_object_across_runs() -> None:
    url = URL("https://example.test/shared")
    runs: list[tuple[int, dict[str, object]]] = [
        (1, {PART: 0, LINK: url}),
        (1, {PART: 1, LINK: url}),
        (1, {PART: 0, LINK: url}),
    ]
    blob = encode_attributed_body_runs("abc", runs)
    assert blob.count(b"NSURL") == 1  # the second dictionary references the first NSURL
    body = parse_attributed_body(blob)
    assert all(r.attributes[LINK] == Url("https://example.test/shared") for r in body.runs)
    assert body.runs[0].attributes is body.runs[2].attributes  # dictionary back-reference


# ---------------------------------------------------------------------------
# UTF-16 offsets and chars()
# ---------------------------------------------------------------------------


def test_utf16_offsets_with_emoji() -> None:
    text = "a\U0001f600b"  # 3 code points, 4 UTF-16 units
    runs: list[tuple[int, dict[str, object]]] = [(1, {PART: 0}), (2, {PART: 1}), (1, {PART: 2})]
    body = parse_attributed_body(encode_attributed_body_runs(text, runs))
    assert [(r.start, r.end) for r in body.runs] == [(0, 1), (1, 3), (3, 4)]
    assert [r.chars(text) for r in body.runs] == ["a", "\U0001f600", "b"]
    assert "".join(r.chars(text) for r in body.runs) == text


def test_chars_rounds_a_boundary_inside_a_surrogate_pair_down() -> None:
    text = "a\U0001f600b"
    runs: list[tuple[int, dict[str, object]]] = [(2, {PART: 0}), (2, {PART: 1})]
    body = parse_attributed_body(encode_attributed_body_runs(text, runs))
    assert [(r.start, r.end) for r in body.runs] == [(0, 2), (2, 4)]
    assert [r.chars(text) for r in body.runs] == ["a", "\U0001f600b"]
    assert "".join(r.chars(text) for r in body.runs) == text


def test_chars_on_bmp_only_text_is_a_plain_slice() -> None:
    text = "שלום עולם"
    runs: list[tuple[int, dict[str, object]]] = [(4, {PART: 0}), (5, {PART: 1})]
    body = parse_attributed_body(encode_attributed_body_runs(text, runs))
    assert [r.chars(text) for r in body.runs] == ["שלום", " עולם"]
    assert AttributeRun(2, 100, {}).chars("abc") == "c"  # end past the text is clamped
    assert AttributeRun(5, 7, {}).chars("abc") == ""


# ---------------------------------------------------------------------------
# message_parts
# ---------------------------------------------------------------------------


def test_message_parts_of_the_three_part_golden() -> None:
    body = parse_attributed_body(golden_three_parts())
    assert message_parts(body) == (
        AttachmentPart("AT_0_0000-FAKE-GUID", 0),
        TextPart(
            " see @Sam https://example.test/x",
            1,
            mentions=(Mention("+15550100", 5, 9),),
            links=("https://example.test/x",),
        ),
    )


def test_message_parts_breadcrumb_run_is_unknown() -> None:
    runs: list[tuple[int, dict[str, object]]] = [
        (2, {PART: 0}),
        (3, {BREADCRUMB_MARKER: "m", BREADCRUMB_FLAGS: Number(1, "q")}),
    ]
    body = parse_attributed_body(encode_attributed_body_runs("hi!!!", runs))
    parts = message_parts(body)
    assert parts[0] == TextPart("hi", 0)
    unknown = parts[1]
    assert isinstance(unknown, UnknownPart)
    assert unknown.part_index is None
    assert plain(unknown.attributes) == {BREADCRUMB_MARKER: "m", BREADCRUMB_FLAGS: 1}


def test_message_parts_empty_text_is_one_unknown_part() -> None:
    body = parse_attributed_body(encode_attributed_body_runs("", [(0, {PART: 0})]))
    assert body.text == "" and body.runs == (AttributeRun(0, 0, {PART: 0}),)
    assert message_parts(body) == (UnknownPart(0, {PART: 0}),)


def test_message_parts_placeholder_without_guid_is_text() -> None:
    runs: list[tuple[int, dict[str, object]]] = [(1, {PART: 0}), (1, {PART: 1})]
    body = parse_attributed_body(encode_attributed_body_runs("￼x", runs))
    assert message_parts(body) == (TextPart("￼", 0), TextPart("x", 1))
    # a guid that is not a string does not make an attachment either
    runs = [(1, {PART: 0, FILE_TRANSFER_GUID: 7})]
    body = parse_attributed_body(encode_attributed_body_runs("￼", runs))
    assert message_parts(body) == (TextPart("￼", 0),)
    # two placeholders in one run are text, not an attachment
    runs = [(2, {PART: 0, FILE_TRANSFER_GUID: "AT_FAKE"})]
    body = parse_attributed_body(encode_attributed_body_runs("￼￼", runs))
    assert message_parts(body) == (TextPart("￼￼", 0),)


def test_message_parts_groups_consecutive_equal_indices_only() -> None:
    runs: list[tuple[int, dict[str, object]]] = [
        (1, {PART: 0}),
        (1, {PART: 0, "__kIMTextBoldAttributeName": 1}),
        (1, {PART: 1}),
        (1, {PART: 0}),  # decreasing index: a new part anyway
    ]
    body = parse_attributed_body(encode_attributed_body_runs("abcd", runs))
    assert message_parts(body) == (TextPart("ab", 0), TextPart("c", 1), TextPart("d", 0))
    # no part attribute at all: one part with part_index None
    runs = [(2, {"__kIMTextBoldAttributeName": 1}), (2, {})]
    body = parse_attributed_body(encode_attributed_body_runs("abcd", runs))
    assert message_parts(body) == (TextPart("abcd", None),)


def test_message_parts_mention_offsets_are_code_points_and_links_deduplicated() -> None:
    text = "\U0001f600 @Al x https://example.test/a https://example.test/a"
    url = URL("https://example.test/a")
    runs: list[tuple[int, dict[str, object]]] = [
        (3, {PART: 0}),  # emoji (2 units) + space
        (3, {PART: 0, MENTION: "+15550100"}),
        (3, {PART: 0}),
        (22, {PART: 0, LINK: url}),
        (1, {PART: 0}),
        (22, {PART: 0, LINK: url, LINK_IS_RICH: True}),
    ]
    body = parse_attributed_body(encode_attributed_body_runs(text, runs))
    (part,) = message_parts(body)
    assert isinstance(part, TextPart)
    assert part.text == text
    assert part.mentions == (Mention("+15550100", 2, 5),)
    assert part.text[2:5] == "@Al"
    assert part.links == ("https://example.test/a",)


def test_part_to_dict_shapes() -> None:
    assert TextPart("t", 1, (Mention("+15550100", 0, 1),), ("u",)).to_dict() == {
        "kind": "text",
        "part_index": 1,
        "text": "t",
        "mentions": [{"handle": "+15550100", "start": 0, "end": 1}],
        "links": ["u"],
    }
    assert AttachmentPart("g", 0).to_dict() == {"kind": "attachment", "part_index": 0, "guid": "g"}
    unknown = UnknownPart(
        None,
        {"b": b"\x00\x01", "u": Url("x"), "c": UnknownValue("NSArray"), "m": {"n": None}, "i": 3},
    )
    assert unknown.to_dict() == {
        "kind": "unknown",
        "part_index": None,
        "attributes": {
            "b": {"base64": "AAE="},
            "u": "x",
            "c": {"class": "NSArray"},
            "m": {"n": None},
            "i": 3,
        },
    }
    json.dumps(unknown.to_dict())  # JSON-ready


# ---------------------------------------------------------------------------
# oracle: parse_attributed_body(blob).text == extract_text(blob)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mutable", [False, True])
@pytest.mark.parametrize("n", [0, 1, 127, 128, 146, 255, 256, 65535, 65536, 70_000])
def test_oracle_lengths(n: int, mutable: bool) -> None:
    text = ("x" * (n - 1) + "Z") if n > 1 else "Z" * n
    for share_keys in (False, True):
        blob = encode_attributed_body_runs(
            text, [(n, {PART: 0})], mutable=mutable, share_keys=share_keys
        )
        body = parse_attributed_body(blob)
        assert body.text == text
        assert extract_text(blob) == (body.text or None)
        assert body.runs == (AttributeRun(0, n, {PART: 0}),)


@pytest.mark.parametrize(
    "text",
    [
        "\U0001f600\U0001f44d",
        "שלום",
        "café — שלום \U0001f600",
        "line1\nline2\ttabbed",
        "￼",
        "a" * 43,  # the length byte equals '+'
        "+ plus + inside + text +",
        "NSString NSAttributedString in the text itself",
        "\U0001f600" * 20,
        "ש" * 3000,
    ],
)
@pytest.mark.parametrize("mutable", [False, True])
def test_oracle_unicode(text: str, mutable: bool) -> None:
    units = len(text.encode("utf-16-le")) // 2
    single = encode_attributed_body_runs(text, [(units, {PART: 0})], mutable=mutable)
    assert parse_attributed_body(single).text == text == extract_text(single)
    # the same text over two runs
    half = units // 2
    split: list[tuple[int, dict[str, object]]] = [(half, {PART: 0}), (units - half, {PART: 1})]
    two = encode_attributed_body_runs(text, split, mutable=mutable)
    body = parse_attributed_body(two)
    assert body.text == text == extract_text(two)
    assert "".join(r.chars(text) for r in body.runs) == text


def test_oracle_invalid_utf8_placed_by_hand() -> None:
    blob = replace_once(
        encode_attributed_body_runs("ok???", [(5, {PART: 0})]), b"\x05ok???", b"\x05ok\xff\xfe\xc3"
    )
    body = parse_attributed_body(blob)
    assert body.text == "ok���" == extract_text(blob)
    assert body.runs == (AttributeRun(0, 5, {PART: 0}),)


def test_oracle_over_the_golden_corpus() -> None:
    for blob in (golden_hi(), golden_hi(mutable=True), golden_three_parts(), golden_data()):
        assert parse_attributed_body(blob).text == extract_text(blob)


def test_empty_text_vs_extract_text_none() -> None:
    blob = encode_attributed_body_runs("", [(0, {PART: 0})])
    assert extract_text(blob) is None
    assert parse_attributed_body(blob).text == ""


def test_unsigned_single_byte_head_0x92_is_a_literal_length() -> None:
    """Section 2.2 (unverified rule): the reader reads 0x92 as 146 where extract_text gives None."""
    text = "x" * 145 + "Z"
    blob = encode_attributed_body_runs(text, [(146, {PART: 0})])
    assert blob.count(b"\x81\x92\x00") == 2  # the '+' length and the 'I' run length
    short = blob.replace(b"\x81\x92\x00", b"\x92")
    body = parse_attributed_body(short)
    assert body.text == text
    assert body.runs == (AttributeRun(0, 146, {PART: 0}),)
    assert extract_text(short) is None


# ---------------------------------------------------------------------------
# truncation, the 0.1 writer, headers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blob", [golden_hi(), golden_three_parts(), golden_data()])
def test_every_prefix_raises_and_never_hangs(blob: bytes) -> None:
    started = time.perf_counter()
    for cut in range(len(blob)):
        prefix = blob[:cut]
        with pytest.raises(TypedStreamError):
            parse_attributed_body(prefix)
        assert try_parse_attributed_body(prefix) is None
    assert time.perf_counter() - started < 30.0
    assert parse_attributed_body(blob).text == extract_text(blob)


def test_prefixes_of_a_large_blob() -> None:
    text = "y" * 69_999 + "Z"
    blob = encode_attributed_body_runs(text, [(70_000, {PART: 0})])
    skeleton = len(PLAIN_SKELETON)
    end_of_text = skeleton + 5 + 70_000
    for cut in (0, 16, skeleton, skeleton + 3, end_of_text - 1, end_of_text, len(blob) - 1):
        with pytest.raises(TypedStreamError):
            parse_attributed_body(blob[:cut])
    assert extract_text(blob[:end_of_text]) == text  # the 0.1 rule is unchanged
    assert parse_attributed_body(blob).text == text


@pytest.mark.parametrize("data", [None, b"", bytearray()])
def test_try_parse_empty_inputs(data: bytes | None) -> None:
    assert try_parse_attributed_body(data) is None


@pytest.mark.parametrize("mutable", [False, True])
@pytest.mark.parametrize("force_len_tag", [None, 0x81, 0x82, 0x83])
@pytest.mark.parametrize("text", ["a", "hello", "x" * 300])
def test_0_1_writer_blobs_are_rejected(text: str, mutable: bool, force_len_tag: int | None) -> None:
    """Section 8: the 0.1 writer's trailer is not what the archiver writes (0x97, (length, 1))."""
    blob = encode_attributed_body(text, mutable=mutable, force_len_tag=force_len_tag)
    assert extract_text(blob) == text
    with pytest.raises(TypedStreamError):
        parse_attributed_body(blob)
    assert try_parse_attributed_body(blob) is None


def test_header_variants() -> None:
    tail = golden_hi()[len(HEADER) :]
    raises(b"\x04\x0btypedstream\x81\xe8\x03" + tail, "streamtyped")  # big-endian
    raises(b"\x03\x0bstreamtyped\x81\xe8\x03" + tail, "streamer version")
    raises(b"\x04\x0bstreamtyped\x81\xe7\x03" + tail, "system version")
    raises(b"\x04\x0bstreamtyped\x05" + tail, "system version")
    raises(b"\x04\x0bstreamtype\x81\xe8\x03" + tail, "streamtyped")
    raises(golden_hi() + b"\x00", "trailing")
    raises(b"", "end of data")
    raises(b"\x00" * 64, "streamer version")
    raises(HEADER + b"\x84\x01i\x05", "root group has type 'i'")


# ---------------------------------------------------------------------------
# the limits of section 9.1 item 7, each hit by a hand-built blob
# ---------------------------------------------------------------------------


def nested_objects(depth: int) -> bytes:
    """``depth`` literal ``NSObject`` instances, each holding the next in an ``@`` field."""
    out = bytearray(HEADER)
    out += b"\x84\x01@"  # S0 = "@"
    for level in range(depth):
        out += b"\x84"  # new object
        # class: literal (0x84 + shared string + version + nil superclass) once, then O1
        out += b"\x84\x84\x08NSObject\x00\x85" if level == 0 else b"\x93"
        out += b"\x92"  # field of type "@"
    out += b"\x85"  # innermost field: nil
    out += b"\x86" * depth
    return bytes(out)


def test_nesting_depth_limit() -> None:
    raises(nested_objects(65), "nesting deeper than 64")
    # 64 levels parse structurally; the root is then rejected for its class
    raises(nested_objects(64), "root object is NSObject")
    started = time.perf_counter()
    raises(nested_objects(5000), "nesting deeper than 64")
    assert time.perf_counter() - started < 10.0


def test_reference_out_of_range_incomplete_and_wrong_kind() -> None:
    blob = golden_hi()
    at = blob.index(b"NSString\x01\x94") + 9  # the NSString superclass reference (O2 = NSObject)
    assert blob[at] == 0x94
    raises(blob[:at] + b"\xa5" + blob[at + 1 :], "out of range")
    raises(blob[:at] + b"\x92" + blob[at + 1 :], "still being read")  # O0: the root, open
    # an object position naming a class (O1 = NSAttributedString)
    at = blob.index(b"\x85\x92\x84\x84\x84\x08NSString") + 2
    raises(blob[:at] + b"\x93" + blob[at + 1 :], "names a Class")
    # a class position naming an object (O3 = the NSString object)
    at = blob.index(b"\x92\x84\x96\x96\x1d") + 2
    raises(blob[:at] + b"\x95" + blob[at + 1 :], "names a Object")
    # a string-table reference out of range
    raises(replace_once(blob, b"\x84\x02iI", b"\xb0"), "string reference")


def test_integer_tags() -> None:
    blob = golden_hi()
    raises(replace_once(blob, b"\x84\x02iI\x01\x02", b"\x84\x02iI\x01\x83"), "0x83")
    for tag in (0x80, 0x87, 0x91):
        raises(replace_once(blob, b"\x84\x02iI\x01\x02", b"\x84\x02iI\x01" + bytes([tag])), "tag")
    wide = replace_once(blob, b"\x84\x02iI\x01\x02", b"\x84\x02iI\x81\x01\x00\x82\x02\x00\x00\x00")
    assert parse_attributed_body(wide) == parse_attributed_body(blob)  # 0x81/0x82 are accepted


def test_type_strings() -> None:
    blob = golden_hi()
    raises(replace_once(blob, b"\x84\x02iI", b"\x84\x02iZ"), "unsupported type character 'Z'")
    raises(replace_once(blob, b"\x84\x02iI", b"\x84\x01{"), "'{'")
    raises(replace_once(blob, b"\x84\x02iI", b"\x84\x01!"), "'!'")
    raises(replace_once(blob, b"\x84\x02iI", b"\x85"), "nil type string")
    data = golden_data()
    raises(replace_once(data, b"\x84\x04[3c]", b"\x84\x04[3i]"), "non-byte")
    raises(replace_once(data, b"\x84\x04[3c]", b"\x84\x03[3c"), "bad array type")
    raises(replace_once(data, b"\x84\x04[3c]", b"\x84\x03[c]"), "bad array type")
    raises(replace_once(data, b"\x84\x04[3c]", b"\x84\x0e[123456789012c]"), "bad array type")
    # A non-ASCII digit in the count (0xb2 is "²" in latin-1, which str.isdigit
    # accepts and int() rejects) is a TypedStreamError, never a ValueError.
    # Found by fuzzing a live blob with one bit flipped inside a type string.
    raises(replace_once(data, b"\x84\x04[3c]", b"\x84\x04[\xb2c]"), "bad array type")
    raises(replace_once(data, b"\x84\x04[3c]", b"\x84\x05[3\xb2c]"), "bad array type")
    raises(replace_once(data, b"\x84\x04[3c]", b"\x84\x05[\xb23c]"), "bad array type")
    started = time.perf_counter()
    raises(replace_once(data, b"\x84\x04[3c]", b"\x84\x0c[999999999c]"), "bytes requested")
    assert time.perf_counter() - started < 10.0


def test_absurd_string_lengths_fail_fast() -> None:
    started = time.perf_counter()
    raises(PLAIN_SKELETON + b"\x82\xff\xff\xff\x7f" + b"hi", "bytes requested")
    raises(PLAIN_SKELETON + b"\x81\xff\xff" + b"hi", "bytes requested")
    raises(PLAIN_SKELETON + b"\x82\xff\xff\xff\xff" + b"hi", "bytes requested")  # u32, not -1
    assert time.perf_counter() - started < 10.0
    empty = encode_attributed_body_runs("", [(0, {PART: 0})])
    raises(replace_once(empty, b"\x84\x01+\x00", b"\x84\x01+\x85"), "nil string")


def test_run_index_rule_and_length_sum() -> None:
    blob = golden_three_parts()
    raises(replace_once(blob, b"\x84\x02iI\x01\x01", b"\x84\x02iI\x02\x01"), "dictionary index 2")
    raises(replace_once(blob, b"\x84\x02iI\x01\x01", b"\x84\x02iI\x00\x01"), "dictionary index 0")
    # run 4 (a back-reference to dictionary 2) given the new index 4 with no dictionary after it
    run4 = b"\x99\x02\x01\x99\x04\x16"
    raises(replace_once(blob, run4, b"\x99\x04\x01\x99\x04\x16"), "no dictionary")
    # ... while index 3 there is a legal back-reference to dictionary 3
    back = parse_attributed_body(replace_once(blob, run4, b"\x99\x03\x01\x99\x04\x16"))
    assert back.runs[3].attributes == {PART: 1, MENTION: "+15550100"}
    # run 3 (new dictionary 3) turned into a back-reference, so its dictionary follows unexpectedly
    raises(replace_once(blob, b"\x99\x03\x04\x92", b"\x99\x02\x04\x92"), "got '@'")
    hi = golden_hi()
    raises(replace_once(hi, b"\x84\x02iI\x01\x02", b"\x84\x02iI\x01\x03"), "run lengths sum to 3")
    raises(replace_once(hi, b"\x84\x02iI\x01\x02", b"\x84\x02iI\x01\x01"), "run lengths sum to 1")


def test_dictionary_and_key_rules() -> None:
    blob = golden_hi()
    raises(replace_once(blob, b"\x84\x01i\x01\x92", b"\x84\x01i\x02\x92"), "pair count 2")
    raises(replace_once(blob, b"NSDictionary\x00", b"NSDictionarz\x00"), "got NSDictionarz")
    # the key's class made NSObject (O2) instead of NSString (O4)
    raises(replace_once(blob, b"\x92\x84\x96\x96\x1d", b"\x92\x84\x94\x96\x1d"), "got NSObject")
    # the key object replaced by nil (later references are unaffected: it added no table entry)
    key = b"\x92\x84\x96\x96\x1d__kIMMessagePartAttributeName\x86"
    raises(replace_once(blob, key, b"\x92\x85"), "got nil")
    # the whole string object replaced by nil; later references then name the wrong entries
    string_object = b"\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+\x02hi\x86"
    raises(replace_once(blob, string_object, b"\x92\x85"), "unsupported type character")
    # a dictionary whose value is a plain integer group ('i' = 5) rather than an object
    number = b"\x84\x84\x84\x08NSNumber\x00\x84\x84\x07NSValue\x00\x94\x84\x01*\x84\x99\x99\x00\x86"
    raises(replace_once(blob, b"\x92" + number, b"\x99\x05"), "not two objects")


def test_nil_attribute_value_is_none() -> None:
    blob = golden_hi()
    number = b"\x84\x84\x84\x08NSNumber\x00\x84\x84\x07NSValue\x00\x94\x84\x01*\x84\x99\x99\x00\x86"
    body = parse_attributed_body(replace_once(blob, number, b"\x85"))
    assert plain(body.runs[0].attributes) == {PART: None}
    assert message_parts(body) == (TextPart("hi", None),)


# ---------------------------------------------------------------------------
# the generic grammar: unknown classes, every scalar type, unexpected layouts
# ---------------------------------------------------------------------------


def with_last_value(value_object: bytes) -> bytes:
    """A two-key blob whose last attribute value is replaced by the hand-written ``value_object``.

    Everything inside ``value_object`` must be literal (``0x84``) so no later
    reference is disturbed; the shared tables simply grow.
    """
    blob = encode_attributed_body_runs("ab", [(2, {PART: 0, RICH_CARDS: "ZZZ"})])
    suffix = b"\x92\x84\x96\x96\x03ZZZ\x86\x86\x86"  # the value, end dictionary, end root
    assert blob.endswith(suffix)
    return blob[: -len(suffix)] + b"\x92" + value_object + b"\x86\x86"


def test_unknown_class_with_every_scalar_type_is_skipped_structurally() -> None:
    value = (
        b"\x84"  # new object
        b"\x84\x84\x07NSArray\x00\x84\x84\x08NSObject\x00\x85"  # class chain, both literal
        b"\x84\x01i\x05"
        b"\x84\x01q\x81\x00\x01"
        b"\x84\x01f\x83\x00\x00\x80\x3f"  # 1.0f
        b"\x84\x01d\x83\x00\x00\x00\x00\x00\x00\xf0\x3f"  # 1.0
        b"\x84\x01d\x07"  # a whole number float written as an integer head
        b"\x84\x01c\x01"
        b"\x84\x01%\x84\x03abc"  # shared string literal
        b"\x84\x01:\x84\x03abc"
        b"\x84\x01*\x84\x84\x01x"  # C string literal
        b"\x84\x01#\x85"  # nil class
        b"\x84\x01#\x84\x84\x03Foo\x02\x84\x84\x03Bar\x00\x85"  # literal class chain Foo > Bar
        b"\x84\x04[2c]\x01\x02"
        b"\x84\x04[0C]"
        b"\x84\x07sSlLQCB\x01\x01\x01\x01\x01\x01\x01"
        b"\x84\x01@\x85"  # nil object
        b"\x84\x00"  # empty type string: no values
        b"\x84\x01@\x84\x84\x84\x08NSObject\x00\x85\x86"  # a nested literal object
        b"\x86"
    )
    body = parse_attributed_body(with_last_value(value))
    assert body.text == "ab"
    assert plain(body.runs[0].attributes) == {PART: 0, RICH_CARDS: UnknownValue("NSArray")}
    assert message_parts(body) == (TextPart("ab", 0),)
    # the same object with a non-byte array, or an unknown character, is rejected
    raises(with_last_value(value.replace(b"[2c]", b"[2i]")), "non-byte")
    raises(with_last_value(value.replace(b"\x84\x01c\x01", b"\x84\x01Z\x01")), "'Z'")
    raises(with_last_value(value.replace(b"\x84\x01i\x05", b"\x84\x01i\x83")), "0x83")
    raises(with_last_value(value[:-1]), "end of data")


def test_known_classes_with_unexpected_layouts_become_unknown_values() -> None:
    nsobject = b"\x84\x84\x08NSObject\x00\x85"  # literal root class
    number_f = (
        b"\x84\x84\x84\x08NSNumber\x00\x84\x84\x07NSValue\x00" + nsobject
        + b"\x84\x01*\x84\x84\x01f\x84\x01f\x83\x00\x00\x80\x3f\x86"
    )
    body = parse_attributed_body(with_last_value(number_f))
    assert body.runs[0].attributes[RICH_CARDS] == UnknownValue("NSNumber")
    url_flag_only = b"\x84\x84\x84\x05NSURL\x00" + nsobject + b"\x84\x01c\x01\x86"
    body = parse_attributed_body(with_last_value(url_flag_only))
    assert body.runs[0].attributes[RICH_CARDS] == UnknownValue("NSURL")
    data_no_bytes = b"\x84\x84\x84\x06NSData\x00" + nsobject + b"\x84\x01i\x00\x86"
    body = parse_attributed_body(with_last_value(data_no_bytes))
    assert body.runs[0].attributes[RICH_CARDS] == UnknownValue("NSData")
    mutable_dict = (
        b"\x84\x84\x84\x13NSMutableDictionary\x00\x84\x84\x0cNSDictionary\x00" + nsobject
        + b"\x84\x01i\x00\x86"
    )
    body = parse_attributed_body(with_last_value(mutable_dict))
    assert body.runs[0].attributes[RICH_CARDS] == UnknownValue("NSMutableDictionary")
    # an NSMutableDictionary *as the run dictionary* is accepted (it is an NSDictionary); the
    # extra class name shifts the string table by one, so the NSNumber's "i" (S7 -> S8) moves
    hi = replace_once(
        golden_hi(),
        b"\x84\x84\x84\x0cNSDictionary\x00\x94",
        b"\x84\x84\x84\x13NSMutableDictionary\x00\x84\x84\x0cNSDictionary\x00\x94",
    )
    hi = replace_once(hi, b"\x84\x01*\x84\x99\x99\x00", b"\x84\x01*\x84\x9a\x9a\x00")
    body = parse_attributed_body(hi)
    assert body.text == "hi" and plain(body.runs[0].attributes) == {PART: 0}


# ---------------------------------------------------------------------------
# fuzz: 2,000 seeded iterations, random bytes and flipped goldens
# ---------------------------------------------------------------------------


def test_fuzz_never_raises_anything_but_typedstream_error() -> None:
    rng = random.Random(0x7E5D)
    corpus = [
        golden_hi(),
        golden_hi(mutable=True),
        golden_three_parts(),
        golden_three_parts(share_keys=True),
        golden_data(),
        encode_attributed_body_runs("ש" * 300 + "\U0001f600", [(302, {PART: 0})]),
        encode_attributed_body_runs("z" * 70_000, [(70_000, {PART: 0})], mutable=True),
        encode_attributed_body_runs("", [(0, {PART: 0})]),
    ]
    heads = (HEADER, HEADER + b"\x84\x01@\x84", b"\x04\x0bstreamtyped", b"NSString")
    started = time.perf_counter()
    outcomes = {"ok": 0, "error": 0}
    for _ in range(2_000):
        kind = rng.random()
        if kind < 0.3:
            n = rng.choice((0, 1, 2, 7, 16, 64, 256, 1024, rng.randrange(0, 5_000)))
            data = rng.randbytes(n)
        elif kind < 0.4:
            data = rng.choice(heads) + rng.randbytes(rng.randrange(0, 512))
        elif kind < 0.45:
            data = bytes([rng.randrange(256)]) * rng.randrange(0, 4_096)
        else:
            flipped = bytearray(rng.choice(corpus))
            action = rng.random()
            if action < 0.7:
                for _ in range(rng.randint(1, 8)):
                    flipped[rng.randrange(len(flipped))] ^= 1 << rng.randrange(8)
            elif action < 0.85:
                del flipped[rng.randrange(len(flipped) + 1) :]
            else:
                i = rng.randrange(len(flipped))
                w = rng.randint(1, 16)
                flipped[i : i + w] = rng.randbytes(w)
            data = bytes(flipped)
        one = time.perf_counter()
        try:
            body = parse_attributed_body(data)
        except TypedStreamError:
            outcomes["error"] += 1
            assert try_parse_attributed_body(data) is None
        else:
            outcomes["ok"] += 1
            assert isinstance(body, AttributedBody)
            assert try_parse_attributed_body(data) == body
            assert sum(r.length for r in body.runs) == len(body.text.encode("utf-16-le")) // 2
            message_parts(body)  # never raises on a parsed body
        assert time.perf_counter() - one < 20.0, data[:32].hex()
    assert outcomes["error"] > 0 and outcomes["ok"] > 0
    assert time.perf_counter() - started < 300.0


# ---------------------------------------------------------------------------
# Message.body / Message.parts
# ---------------------------------------------------------------------------


def test_message_fields_are_additive() -> None:
    fields = {f.name: f for f in dataclasses.fields(Message)}
    assert fields["attributed_body_raw"].default is None
    for cache in ("_body_cache", "_parts_cache"):
        assert fields[cache].init is False
        assert fields[cache].compare is False
        assert fields[cache].repr is False


def test_message_body_and_parts_from_a_fixture_row(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    chat = b.add_chat(fx.writer, CHAT, 45, handles=[PHONE])
    blob = golden_three_parts()
    b.add_message(fx.writer, chat, body=blob, handle=PHONE)
    b.add_message(fx.writer, chat, text="column wins", body=golden_hi(), handle=PHONE)
    b.add_message(fx.writer, chat, text="no blob", handle=PHONE)
    b.add_message(fx.writer, chat, body=encode_attributed_body("0.1 writer"), handle=PHONE)
    three, column, plain_text, old = fx.db.messages_after(0)

    assert three.attributed_body_raw == blob
    assert three.text == THREE_PARTS_TEXT
    assert three.body is not None and three.body.text == THREE_PARTS_TEXT
    assert three.body is three.body  # cached
    assert three.parts is three.parts
    assert three.parts == (
        AttachmentPart("AT_0_0000-FAKE-GUID", 0),
        TextPart(
            " see @Sam https://example.test/x",
            1,
            mentions=(Mention("+15550100", 5, 9),),
            links=("https://example.test/x",),
        ),
    )
    # the column wins in ``text``; ``body.text`` is the blob's string
    assert column.text == "column wins"
    assert column.body is not None and column.body.text == "hi"
    assert column.parts == (TextPart("hi", 0),)
    # no blob at all
    assert plain_text.attributed_body_raw is None
    assert plain_text.body is None and plain_text.parts == ()
    # a blob the reader rejects still has its byte-scan text
    assert old.text == "0.1 writer"
    assert old.body is None and old.parts == ()


def test_message_to_dict_eq_repr_and_replace_ignore_the_caches(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    chat = b.add_chat(fx.writer, CHAT, 45, handles=[PHONE])
    b.add_message(fx.writer, chat, body=golden_three_parts(), handle=PHONE)
    (m,) = fx.db.messages_after(0)
    (same,) = fx.db.messages_after(0)
    d = m.to_dict()
    assert "parts" not in d and "attributed_body_raw" not in d and "body" not in d
    assert list(d) == [
        "rowid", "guid", "text", "date", "date_read", "date_edited", "is_from_me",
        "sender", "sender_handle", "chat_guid", "chat_name", "is_group", "has_attachments",
        "assoc_guid", "assoc_type", "attachments", "link", "reply_to_guid", "reply_to",
        "service",
    ]  # fmt: skip
    assert m.body is not None  # populate m's cache only
    assert m == same
    assert hash(m) == hash(same)
    assert "_body_cache" not in repr(m) and "mappingproxy" not in repr(m)
    copy = dataclasses.replace(m, enriched=True)
    assert copy.body == m.body and copy.parts == m.parts
    assert copy.attributed_body_raw == m.attributed_body_raw


def test_package_exports() -> None:
    names = [
        "TypedStreamError", "AttributedBody", "AttributeRun", "Url", "UnknownValue", "Mention",
        "TextPart", "AttachmentPart", "UnknownPart", "Part", "parse_attributed_body",
        "try_parse_attributed_body", "message_parts",
    ]  # fmt: skip
    for name in names:
        assert name in imessage_chatdb.__all__
        assert getattr(imessage_chatdb, name) is getattr(imessage_chatdb.typedstream_reader, name)


# ---------------------------------------------------------------------------
# CLI --parts
# ---------------------------------------------------------------------------


def run_cli(argv: list[str]) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, stdout=out, stderr=err)
    assert err.getvalue() == ""
    return code, out.getvalue()


def test_cli_parts_flag_defaults_to_false() -> None:
    parser = build_parser()
    assert parser.parse_args(["tail"]).parts is False
    assert parser.parse_args(["tail", "--parts"]).parts is True
    assert parser.parse_args(["search", "x"]).parts is False
    assert parser.parse_args(["search", "x", "--parts"]).parts is True


def test_cli_tail_and_search_parts(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    chat = b.add_chat(fx.writer, CHAT, 45, handles=[PHONE])
    b.add_message(fx.writer, chat, text="plain text, see", handle=PHONE)
    b.add_message(fx.writer, chat, body=golden_three_parts(), handle=PHONE)
    b.add_message(fx.writer, chat, body=encode_attributed_body("0.1 see"), handle=PHONE)
    db = str(fx.path)
    expected_parts: list[dict[str, Any]] = [
        {"kind": "attachment", "part_index": 0, "guid": "AT_0_0000-FAKE-GUID"},
        {
            "kind": "text",
            "part_index": 1,
            "text": " see @Sam https://example.test/x",
            "mentions": [{"handle": "+15550100", "start": 5, "end": 9}],
            "links": ["https://example.test/x"],
        },
    ]
    for argv in (["tail", "--since-rowid", "0"], ["search", "see"]):
        code, without = run_cli([*argv, "--db", db])
        assert code == EXIT_OK
        code, with_parts = run_cli([*argv, "--parts", "--db", db])
        assert code == EXIT_OK
        plain_lines = without.splitlines()
        parts_lines = with_parts.splitlines()
        assert len(plain_lines) == len(parts_lines) == 3
        by_rowid: dict[int, list[dict[str, Any]]] = {}
        for raw_plain, raw_parts in zip(plain_lines, parts_lines, strict=True):
            d_plain, d_parts = json.loads(raw_plain), json.loads(raw_parts)
            assert "parts" not in d_plain
            assert list(d_parts)[-1] == "parts"
            parts = d_parts.pop("parts")
            assert d_parts == d_plain and list(d_parts) == list(d_plain)
            # the line without --parts is exactly the 0.1.1 serialization
            assert raw_plain == json.dumps(d_plain, ensure_ascii=False)
            by_rowid[d_plain["rowid"]] = parts
        assert sorted(by_rowid) == [1, 2, 3]
        assert by_rowid[1] == []  # no blob
        assert by_rowid[2] == expected_parts
        assert by_rowid[3] == []  # 0.1-writer blob: the reader rejects it, text still printed


# ---------------------------------------------------------------------------
# 0.2.0 hardening: every depth bounded by counting (never RecursionError), one
# conversion per archived object, message_parts linear in text + runs
# ---------------------------------------------------------------------------


def literal_class_chain(n: int, *, distinct_names: bool = False) -> bytes:
    """A root object whose class chain is ``n`` literal classes.

    With ``distinct_names`` every class name is its own literal string; otherwise
    the first is literal (``S1 = "A"``) and the rest name it by S-reference.
    """
    if distinct_names:
        classes = b"\x84\x84\x01A\x00" * n
    else:
        classes = b"\x84\x84\x01A\x00" + b"\x84\x93\x00" * (n - 1)
    return HEADER + b"\x84\x01@\x84" + classes + b"\x85\x86"


def class_chain_extended_by_reference(n: int) -> bytes:
    """Root with an ``n``-class chain, holding an object whose class ``B`` inherits it (O1)."""
    root_chain = b"\x84\x84\x01A\x00" + b"\x84\x93\x00" * (n - 1) + b"\x85"
    child = b"\x92\x84\x84\x84\x01B\x00\x93\x86"  # '@' field: new object, class B > O1
    return HEADER + b"\x84\x01@\x84" + root_chain + child + b"\x86"


def _dictionary_with_reference(a: Any, slots: list[int], *, value: object) -> None:
    """Archive ``{"k": value}`` where ``value`` is ``"v"`` or an O-reference to ``slots[-1]``."""
    slots.append(a.objects)
    a.begin_object(_CHAIN_DICTIONARY)
    a.typed(b"i")
    a.out += encode_sint(1)
    a.typed(b"@")
    a.string_object("k", key=True)
    a.typed(b"@")
    if value == "ref":
        a.reference(slots[-2])
    else:
        a.string_object("v")
    a.end_object()


def dictionary_reference_chain(n: int) -> bytes:
    """``n`` runs; run k's dictionary is ``{"k": <reference to run k-1's dictionary>}``.

    Every dictionary is converted in run order, so the chain is built from
    cached conversions: the nesting cap is met on the cached-height path.
    """
    a = _Archiver(share_keys=True, share_values=True)
    a.out += b"\x04\x0bstreamtyped" + encode_int(1000)
    a.typed(b"@")
    a.begin_object(_CHAIN_ATTRIBUTED)
    a.typed(b"@")
    a.string_object("x" * n)
    slots: list[int] = []
    for k in range(1, n + 1):
        a.typed(b"iI")
        a.out += encode_sint(k) + encode_int(1)
        a.typed(b"@")
        _dictionary_with_reference(a, slots, value="v" if k == 1 else "ref")
    a.end_object()
    return bytes(a.out)


def dictionary_chain_behind_an_array(n: int) -> bytes:
    """Run 1 holds an ``NSArray`` of ``n`` chained dictionaries (parsed, never
    converted); run 2's dictionary references the last of them, so converting
    it walks the whole chain uncached: the nesting cap is met on the
    not-yet-converted path, at recursion depth ``_MAX_DEPTH`` at most."""
    a = _Archiver(share_keys=True, share_values=True)
    a.out += b"\x04\x0bstreamtyped" + encode_int(1000)
    a.typed(b"@")
    a.begin_object(_CHAIN_ATTRIBUTED)
    a.typed(b"@")
    a.string_object("xy")
    a.typed(b"iI")
    a.out += encode_sint(1) + encode_int(1)
    a.typed(b"@")
    a.begin_object(_CHAIN_DICTIONARY)
    a.typed(b"i")
    a.out += encode_sint(1)
    a.typed(b"@")
    a.string_object("list", key=True)
    a.typed(b"@")
    a.begin_object((("NSArray", 0), ("NSObject", 0)))
    a.typed(b"i")
    a.out += encode_sint(n)
    slots: list[int] = []
    for k in range(1, n + 1):
        a.typed(b"@")
        _dictionary_with_reference(a, slots, value="v" if k == 1 else "ref")
    a.end_object()  # NSArray
    a.end_object()  # run 1 dictionary
    a.typed(b"iI")
    a.out += encode_sint(2) + encode_int(1)
    a.typed(b"@")
    _dictionary_with_reference(a, slots, value="ref")
    a.end_object()
    return bytes(a.out)


def nested_depth(value: object) -> int:
    depth = 0
    while isinstance(value, Mapping):
        depth += 1
        value = value["k"]
    return depth


def test_class_chain_is_read_iteratively_and_capped() -> None:
    raises(literal_class_chain(64), "root object is A")  # 64 classes parse structurally
    raises(literal_class_chain(65), "class chain longer than 64")
    started = time.perf_counter()
    for blob in (
        literal_class_chain(1000),
        literal_class_chain(5000, distinct_names=True),
        literal_class_chain(20_000),
    ):
        raises(blob, "class chain longer than 64")  # never RecursionError
    assert time.perf_counter() - started < 10.0
    # the cap is on the whole chain, references included
    raises(class_chain_extended_by_reference(63), "root object is A")  # B > 63 = 64
    raises(class_chain_extended_by_reference(64), "class chain longer than 64")


def test_dictionary_reference_chain_is_capped_without_recursion() -> None:
    body = parse_attributed_body(dictionary_reference_chain(64))
    assert body.text == "x" * 64
    assert nested_depth(body.runs[-1].attributes) == 64
    assert body.runs[-1].attributes["k"] is body.runs[-2].attributes  # one conversion, shared
    raises(dictionary_reference_chain(65), "nested deeper than 64")
    started = time.perf_counter()
    raises(dictionary_reference_chain(5000), "nested deeper than 64")  # never RecursionError
    raises(dictionary_reference_chain(50_000), "nested deeper than 64")
    assert time.perf_counter() - started < 20.0


def test_uncached_dictionary_chain_is_capped_without_recursion() -> None:
    body = parse_attributed_body(dictionary_chain_behind_an_array(63))
    assert plain(body.runs[0].attributes) == {"list": UnknownValue("NSArray")}
    assert nested_depth(body.runs[1].attributes) == 64  # run 2's own mapping + 63 behind it
    raises(dictionary_chain_behind_an_array(64), "nested deeper than 64")
    started = time.perf_counter()
    raises(dictionary_chain_behind_an_array(5000), "nested deeper than 64")
    assert time.perf_counter() - started < 20.0


def test_literal_dictionary_nesting_still_meets_the_object_cap() -> None:
    # the run dictionary is object-nesting level 2 (root, then it) and the innermost
    # mapping's key/value strings are one level below it, so 62 literal mappings fit
    # under the 64-object cap and the 63rd meets the literal cap, not the mapping cap
    nested: dict[str, object] = {"k": "v"}
    for _ in range(61):
        nested = {"k": nested}
    body = parse_attributed_body(encode_attributed_body_runs("a", [(1, nested)]))
    assert nested_depth(body.runs[0].attributes) == 62
    raises(encode_attributed_body_runs("a", [(1, {"k": nested})]), "nesting deeper than 64")


def test_shared_objects_are_converted_once() -> None:
    # one 100 KB NSString referenced as the value of 1,000 keys: decoded once
    big = "S" * 100_000
    blob = encode_attributed_body_runs("a", [(1, {f"k{i}": big for i in range(1000)})])
    assert blob.count(b"S" * 100_000) == 1
    started = time.perf_counter()
    body = parse_attributed_body(blob)
    assert time.perf_counter() - started < 10.0
    values = list(body.runs[0].attributes.values())
    assert len(values) == 1000 and all(v is values[0] for v in values)
    # dictionary A with n pairs referenced n times from dictionary B: A converted once
    n = 2000
    a = _Archiver(share_keys=False, share_values=False)
    a.out += b"\x04\x0bstreamtyped" + encode_int(1000)
    a.typed(b"@")
    a.begin_object(_CHAIN_ATTRIBUTED)
    a.typed(b"@")
    a.string_object("ab")
    a.typed(b"iI")
    a.out += encode_sint(1) + encode_int(1)
    a.typed(b"@")
    slot_a = a.objects
    a.dictionary_object({f"k{i}": f"v{i}" for i in range(n)})
    a.typed(b"iI")
    a.out += encode_sint(2) + encode_int(1)
    a.typed(b"@")
    a.begin_object(_CHAIN_DICTIONARY)
    a.typed(b"i")
    a.out += encode_sint(n)
    for i in range(n):
        a.typed(b"@")
        a.string_object(f"j{i}", key=True)
        a.typed(b"@")
        a.reference(slot_a)
    a.end_object()
    a.end_object()
    started = time.perf_counter()
    body = parse_attributed_body(bytes(a.out))
    assert time.perf_counter() - started < 10.0
    first = body.runs[0].attributes
    assert len(first) == n
    assert all(v is first for v in body.runs[1].attributes.values())


def test_message_parts_is_linear_in_text_and_runs() -> None:
    n, runs_count = 200_000, 2_000
    runs: list[tuple[int, dict[str, object]]] = [
        (n // runs_count, {PART: i}) for i in range(runs_count)
    ]
    body = parse_attributed_body(encode_attributed_body_runs("a" * n, runs))
    started = time.perf_counter()
    parts = message_parts(body)
    assert time.perf_counter() - started < 10.0
    assert len(parts) == runs_count
    assert "".join(p.text for p in parts if isinstance(p, TextPart)) == body.text
    # the same with astral text (two units per code point)
    emoji = "\U0001f600" * (n // 2)
    body = parse_attributed_body(encode_attributed_body_runs(emoji, runs))
    started = time.perf_counter()
    parts = message_parts(body)
    assert time.perf_counter() - started < 10.0
    assert "".join(p.text for p in parts if isinstance(p, TextPart)) == emoji


def test_run_texts_match_chars_on_boundaries_inside_surrogate_pairs() -> None:
    from imessage_chatdb.typedstream_reader import _run_texts

    text = "a\U0001f600b\U0001f44d\U0001f600c"
    units = len(text.encode("utf-16-le")) // 2
    rng = random.Random(7)
    for _ in range(200):
        cuts = sorted(rng.randrange(0, units + 1) for _ in range(rng.randrange(0, 6)))
        bounds = [0, *cuts, units]
        pairs = zip(bounds, bounds[1:], strict=False)  # one shorter by design
        runs = tuple(AttributeRun(s, e, {}) for s, e in pairs)
        assert _run_texts(text, runs) == [r.chars(text) for r in runs]
    odd = (AttributeRun(-3, 2, {}), AttributeRun(2, 99, {}), AttributeRun(5, 1, {}))
    # unit 2 is the second half of the first pair: both boundaries round down to it
    expected = ["a", "\U0001f600b\U0001f44d\U0001f600c", ""]
    assert _run_texts(text, odd) == [r.chars(text) for r in odd] == expected
    assert AttributeRun(-2, 1, {}).chars("abc") == "a"  # a negative start clamps to 0


def test_known_classes_with_bad_layouts_are_unknown_values_in_value_position() -> None:
    nsobject = b"\x84\x84\x08NSObject\x00\x85"
    nsstring = b"\x84\x84\x84\x08NSString\x01" + nsobject
    nsdict = b"\x84\x84\x84\x0cNSDictionary\x00" + nsobject
    nsurl = b"\x84\x84\x84\x05NSURL\x00" + nsobject
    cases = {
        "NSString": [
            nsstring + b"\x84\x01+\x85\x86",  # nil '+'
            nsstring + b"\x86",  # no field at all
            nsstring + b"\x84\x01+\x01a\x84\x01+\x01b\x86",  # two '+' fields
            nsstring + b"\x84\x01i\x05\x86",  # an 'i' field instead
        ],
        "NSDictionary": [
            nsdict + b"\x84\x01i\x05\x86",  # count 5, no pairs
            nsdict + b"\x86",  # no count
            nsdict + b"\x84\x01i\x01\x84\x01@\x85\x84\x01@\x85\x86",  # nil key
            nsdict + b"\x84\x01i\x01\x84\x01i\x01\x84\x01i\x02\x86",  # pair not objects
        ],
        "NSURL": [
            nsurl + b"\x84\x01c\x00\x84\x01@" + nsstring + b"\x84\x01+\x85\x86\x86",  # bad string
        ],
    }
    for class_name, values in cases.items():
        for value in values:
            body = parse_attributed_body(with_last_value(value))
            assert body.text == "ab"
            assert plain(body.runs[0].attributes) == {PART: 0, RICH_CARDS: UnknownValue(class_name)}
            assert message_parts(body) == (TextPart("ab", 0),), value
    # a nested dictionary keeps its good pairs; only the bad value is unknown
    nested = (
        nsdict + b"\x84\x01i\x01"
        b"\x84\x01@" + nsstring + b"\x84\x01+\x01z\x86"
        b"\x84\x01@" + nsstring + b"\x84\x01+\x85\x86\x86"
    )
    body = parse_attributed_body(with_last_value(nested))
    assert plain(body.runs[0].attributes) == {PART: 0, RICH_CARDS: {"z": UnknownValue("NSString")}}
    # the strict positions are unchanged: the text, a key and a run dictionary must be well formed
    empty = encode_attributed_body_runs("", [(0, {PART: 0})])
    raises(replace_once(empty, b"\x84\x01+\x00", b"\x84\x01+\x85"), "nil string")
    hi = golden_hi()
    raises(replace_once(hi, b"\x84\x01i\x01\x92", b"\x84\x01i\x02\x92"), "pair count 2")
    raises(replace_once(hi, b"\x92\x84\x96\x96\x1d", b"\x92\x84\x94\x96\x1d"), "got NSObject")


def test_try_parse_swallows_a_recursion_error_backstop(monkeypatch: pytest.MonkeyPatch) -> None:
    from imessage_chatdb import typedstream_reader as reader_mod

    def boom(_data: bytes) -> AttributedBody:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(reader_mod, "parse_attributed_body", boom)
    assert reader_mod.try_parse_attributed_body(golden_hi()) is None


def test_message_and_cli_parts_never_raise_on_crafted_blobs(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    chat = b.add_chat(fx.writer, CHAT, 45, handles=[PHONE])
    crafted = [
        literal_class_chain(5000, distinct_names=True),
        literal_class_chain(20_000),
        dictionary_reference_chain(5000),
        dictionary_chain_behind_an_array(5000),
    ]
    for blob in crafted:
        b.add_message(fx.writer, chat, body=blob, handle=PHONE)
    messages = fx.db.messages_after(0)
    assert len(messages) == len(crafted)
    for m in messages:
        assert m.body is None and m.parts == ()
    code, out = run_cli(["tail", "--since-rowid", "0", "--parts", "--db", str(fx.path)])
    assert code == EXIT_OK
    lines = [json.loads(line) for line in out.splitlines()]
    assert [d["rowid"] for d in lines] == [m.rowid for m in messages]
    assert all(d["parts"] == [] for d in lines)
