"""Fabricate ``message.attributedBody`` blobs (DESIGN.md section 8.2, docs/TYPEDSTREAM.md).

Two writers live here.

``encode_attributed_body`` (0.1) is the single-string writer the ``extract_text``
pins are built on.  It is kept byte-for-byte as it shipped.  Its skeletons were
checked against the live layouts in 0.1, but the 0.2 survey of every live blob
(docs/TYPEDSTREAM.md section 7) found three places where its output is **not**
what Apple's archiver writes: ``MUTABLE_SKELETON`` roots the archive at
``NSAttributedString`` where real mutable blobs root at
``NSMutableAttributedString``; the two ``iI`` integers after the text are
written as (run length, 1) where the archiver writes (dictionary index 1, run
length); and ``TRAILER_TAIL`` carries ``0x97`` where the archiver writes
``0x96`` for the ``+`` type reference of the attribute-name string.  None of
this matters to the byte-scan ``extract_text``, which is why the 0.1 pins hold,
but a real typedstream reader rejects those blobs: use the second writer for
anything that goes through ``parse_attributed_body``.

``encode_attributed_body_runs`` (0.2) is a small NSArchiver: it keeps the two
shared tables (strings; objects/classes/C strings), emits ``0x84`` for a first
occurrence and a ``0x92``-based reference for every later one, and writes the
attribute runs as (dictionary index, run length) pairs with each distinct
dictionary archived once.  Re-encoding every live blob from its parsed
structure reproduces the live bytes exactly (counts in docs/TYPEDSTREAM.md
section 7), so blobs from this writer are what the reader is tested against.

Only synthetic text is ever encoded here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "PLAIN_SKELETON",
    "MUTABLE_SKELETON",
    "MUTABLE_ROOT_SKELETON",
    "TRAILER_HEAD",
    "TRAILER_TAIL",
    "LEN_TAGS",
    "PART",
    "FILE_TRANSFER_GUID",
    "WRITING_DIRECTION",
    "MENTION",
    "LINK",
    "LINK_IS_RICH",
    "DATA_DETECTED",
    "ONE_TIME_CODE",
    "TEXT_BOLD",
    "Number",
    "URL",
    "Data",
    "encode_int",
    "encode_sint",
    "encode_attributed_body",
    "encode_attributed_body_runs",
]

PLAIN_SKELETON = (
    b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00"
    b"\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
)
#: The 0.1 mutable skeleton.  Kept for the 0.1 golden tests; matches no live
#: blob (0 of 6,062 mutable blobs surveyed) -- see the module docstring.
MUTABLE_SKELETON = (
    b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00"
    b"\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x0fNSMutableString\x01"
    b"\x84\x84\x08NSString\x01\x95\x84\x01+"
)
#: The mutable layout Apple's archiver writes (exact prefix of 6,062 of 6,062
#: live ``NSMutableAttributedString`` blobs): the root class chain is
#: ``NSMutableAttributedString > NSAttributedString > NSObject`` and the string
#: is ``NSMutableString > NSString > NSObject``, whose ``NSObject`` superclass
#: is the reference ``0x95`` (object-table index 3).
MUTABLE_ROOT_SKELETON = (
    b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x19NSMutableAttributedString\x00"
    b"\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x0fNSMutableString\x01"
    b"\x84\x84\x08NSString\x01\x95\x84\x01+"
)

# Byte right after the string, then the start of the attribute run: "84 02 69 49".
TRAILER_HEAD = b"\x86\x84\x02iI"
# After run length + attribute count: one __kIMMessagePartAttributeName = 0 attribute.
TRAILER_TAIL = (
    b"\x92\x84\x84\x84\x0cNSDictionary\x00\x94\x84\x01i\x01\x92\x84\x96\x97"
    b"\x1d__kIMMessagePartAttributeName\x86\x92\x84\x84\x84\x08NSNumber\x00"
    b"\x84\x84\x07NSValue\x00\x94\x84\x01*\x84\x99\x99\x00\x86\x86\x86"
)

# tag -> payload width in bytes (little-endian unsigned)
LEN_TAGS: dict[int, int] = {0x81: 2, 0x82: 4, 0x83: 8}

_ATTRIBUTE_COUNT = 1

# Attribute keys seen on the live database (docs/TYPEDSTREAM.md section 6).
PART = "__kIMMessagePartAttributeName"
FILE_TRANSFER_GUID = "__kIMFileTransferGUIDAttributeName"
WRITING_DIRECTION = "__kIMBaseWritingDirectionAttributeName"
MENTION = "__kIMMentionConfirmedMention"
LINK = "__kIMLinkAttributeName"
LINK_IS_RICH = "__kIMLinkIsRichLinkAttributeName"
DATA_DETECTED = "__kIMDataDetectedAttributeName"
ONE_TIME_CODE = "__kIMOneTimeCodeAttributeName"
TEXT_BOLD = "__kIMTextBoldAttributeName"


def encode_int(value: int, *, force_tag: int | None = None) -> bytes:
    """Typedstream unsigned integer: one byte ``< 0x80``, else a tag + LE payload.

    ``force_tag`` emits that wider tag even for a value that would fit in fewer
    bytes.  A tag too narrow for ``value`` or an unknown tag raises ``ValueError``.
    (``0x83`` is a float tag in the public typedstream description; the
    ``0x83`` + u64 form here exists only to exercise ``extract_text``'s
    8-byte read and is never written by Apple's archiver.)
    """
    if value < 0:
        raise ValueError(f"typedstream integers are unsigned here, got {value}")
    if force_tag is None:
        if value < 0x80:
            return bytes([value])
        for tag, width in LEN_TAGS.items():
            if value < 1 << (8 * width):
                return bytes([tag]) + value.to_bytes(width, "little")
        raise ValueError(f"{value} does not fit in a u64 length")
    forced_width = LEN_TAGS.get(force_tag)
    if forced_width is None:
        raise ValueError(f"unknown length tag {force_tag:#x}; expected one of {sorted(LEN_TAGS)}")
    if value >= 1 << (8 * forced_width):
        raise ValueError(
            f"{value} does not fit in the {forced_width}-byte payload of tag {force_tag:#x}"
        )
    return bytes([force_tag]) + value.to_bytes(forced_width, "little")


def encode_sint(value: int) -> bytes:
    """Typedstream signed integer (``i``/``q`` values, class versions, references).

    One byte when the value fits a signed byte **and** is outside the tag range
    ``-128..-111`` (``0x80..0x91``): so ``-1`` is ``0xff`` and ``5`` is ``0x05``.
    Otherwise ``0x81`` + int16 LE, then ``0x82`` + int32 LE.  Wider values raise
    ``ValueError`` (``0x83`` is the float tag; no int64 tag is known).
    """
    if -128 <= value <= 127 and not -128 <= value <= -111:
        return bytes([value & 0xFF])
    if -0x8000 <= value <= 0x7FFF:
        return b"\x81" + value.to_bytes(2, "little", signed=True)
    if -0x8000_0000 <= value <= 0x7FFF_FFFF:
        return b"\x82" + value.to_bytes(4, "little", signed=True)
    raise ValueError(f"{value} does not fit a typedstream int32")


def encode_attributed_body(
    text: str, *, mutable: bool = False, force_len_tag: int | None = None
) -> bytes:
    """Build the 0.1 single-string ``attributedBody`` blob carrying ``text``.

    ``mutable`` selects the ``NSMutableAttributedString`` skeleton.
    ``force_len_tag`` (``0x81``, ``0x82`` or ``0x83``) widens the text LEN tag for
    a short string so the decoder's wide-tag paths can be exercised.

    Kept as it shipped in 0.1 (see the module docstring for the three ways its
    trailer departs from the live bytes); ``extract_text`` reads it, the
    typedstream reader does not.  New tests use :func:`encode_attributed_body_runs`.
    """
    skeleton = MUTABLE_SKELETON if mutable else PLAIN_SKELETON
    utf8 = text.encode("utf-8")
    run_units = len(text.encode("utf-16-le")) // 2
    return (
        skeleton
        + encode_int(len(utf8), force_tag=force_len_tag)
        + utf8
        + TRAILER_HEAD
        + encode_int(run_units)
        + encode_int(_ATTRIBUTE_COUNT)
        + TRAILER_TAIL
    )


# ---------------------------------------------------------------------------
# 0.2 writer: a small NSArchiver
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Number:
    """An ``NSNumber`` with an explicit Objective-C type: ``"i"``, ``"q"`` or ``"c"``.

    A bare ``int`` attribute value is ``Number(value, "i")``; a bare ``bool`` is
    ``Number(int(value), "c")``.  ``"c"`` is written as one raw byte, ``"i"`` and
    ``"q"`` as a signed typedstream integer.
    """

    value: int
    objc_type: str = "i"


@dataclass(frozen=True, slots=True)
class URL:
    """An ``NSURL`` attribute value: ``c`` flag byte 0, then the string as ``NSString``."""

    relative: str


@dataclass(frozen=True, slots=True)
class Data:
    """An ``NSData`` (or ``NSMutableData`` when ``mutable``) attribute value."""

    payload: bytes
    mutable: bool = False


AttributeValue = (
    int | str | bytes | Number | URL | Data | Mapping[str, object]
)  # documented union; Mapping values are the same union

_TAG_NEW = b"\x84"
_TAG_NIL = b"\x85"
_TAG_END = b"\x86"
_FIRST_REFERENCE = -110  # 0x92 as a signed byte

_NSOBJECT = ("NSObject", 0)
_NSSTRING = ("NSString", 1)
_CHAIN_ATTRIBUTED = (("NSAttributedString", 0), _NSOBJECT)
_CHAIN_MUTABLE_ATTRIBUTED = (("NSMutableAttributedString", 0), ("NSAttributedString", 0), _NSOBJECT)
_CHAIN_STRING = (_NSSTRING, _NSOBJECT)
_CHAIN_MUTABLE_STRING = (("NSMutableString", 1), _NSSTRING, _NSOBJECT)
_CHAIN_DICTIONARY = (("NSDictionary", 0), _NSOBJECT)
_CHAIN_NUMBER = (("NSNumber", 0), ("NSValue", 0), _NSOBJECT)
_CHAIN_URL = (("NSURL", 0), _NSOBJECT)
_CHAIN_DATA = (("NSData", 0), _NSOBJECT)
_CHAIN_MUTABLE_DATA = (("NSMutableData", 0), ("NSData", 0), _NSOBJECT)


class _Archiver:
    """Byte emitter with the archiver's two shared tables.

    ``strings`` maps a shared string (type strings, class names, C strings) to
    its index; ``objects`` counts object-table slots (objects, classes and
    literal C strings, numbered in stream order) and ``classes`` /
    ``cstrings`` remember which of those slots hold a class or a C string so a
    repeat is emitted as a reference.
    """

    def __init__(self, *, share_keys: bool, share_values: bool) -> None:
        self.share_keys = share_keys
        self.share_values = share_values
        self.out = bytearray()
        self.strings: dict[bytes, int] = {}
        self.objects = 0
        self.classes: dict[tuple[str, int], int] = {}
        self.cstrings: dict[bytes, int] = {}
        # Value objects already archived, by (kind, value).  Apple's archiver
        # shares an object it has already written *by pointer* (a tagged
        # NSNumber, the NSURL a data detector attached to several runs, a key
        # string that is the same constant); value equality under the two
        # ``share_*`` flags is the closest a byte-level writer can get
        # (docs/TYPEDSTREAM.md section 7 has the counts).
        self.values: dict[object, int] = {}

    # ----- primitives
    def reference(self, index: int) -> None:
        self.out += encode_sint(index + _FIRST_REFERENCE)

    def shared_string(self, raw: bytes) -> None:
        index = self.strings.get(raw)
        if index is None:
            self.strings[raw] = len(self.strings)
            self.out += _TAG_NEW + encode_int(len(raw)) + raw
        else:
            self.reference(index)

    def typed(self, type_string: bytes) -> None:
        """Start a typed-values group: the type string (shared)."""
        self.shared_string(type_string)

    def c_string(self, raw: bytes) -> None:
        """A ``*`` value: ``0x84`` + shared string on first use, else an object reference."""
        index = self.cstrings.get(raw)
        if index is None:
            self.cstrings[raw] = self.objects
            self.objects += 1
            self.out += _TAG_NEW
            self.shared_string(raw)
        else:
            self.reference(index)

    # ----- objects
    def begin_object(self, chain: tuple[tuple[str, int], ...]) -> None:
        self.out += _TAG_NEW
        self.objects += 1  # the object's slot is taken before its class
        for depth, cls in enumerate(chain):
            known = self.classes.get(cls)
            if known is not None:
                self.reference(known)
                return
            self.out += _TAG_NEW
            self.shared_string(cls[0].encode("ascii"))
            self.out += encode_sint(cls[1])
            self.classes[cls] = self.objects
            self.objects += 1
            if depth == len(chain) - 1:
                self.out += _TAG_NIL  # root of the chain has no superclass

    def end_object(self) -> None:
        self.out += _TAG_END

    def shared(self, key: object, *, enabled: bool) -> bool:
        """Emit a reference and return True when ``key`` was archived before.

        Otherwise remember that the next ``begin_object`` slot holds it.  With
        ``enabled`` False nothing is looked up or remembered: the object is
        archived afresh every time, as the archiver does for a distinct pointer.
        """
        if not enabled:
            return False
        index = self.values.get(key)
        if index is not None:
            self.reference(index)
            return True
        self.values[key] = self.objects
        return False

    def string_object(self, text: str, *, mutable: bool = False, key: bool = False) -> None:
        enabled = self.share_keys if key else self.share_values
        if self.shared(("str", mutable, text), enabled=enabled):
            return
        self.begin_object(_CHAIN_MUTABLE_STRING if mutable else _CHAIN_STRING)
        self.typed(b"+")
        utf8 = text.encode("utf-8")
        self.out += encode_int(len(utf8)) + utf8
        self.end_object()

    def number_object(self, number: Number) -> None:
        objc = number.objc_type.encode("ascii")
        if objc not in (b"i", b"q", b"c"):
            raise ValueError(f"unsupported NSNumber type {number.objc_type!r}")
        if self.shared(("num", number.objc_type, number.value), enabled=self.share_values):
            return
        self.begin_object(_CHAIN_NUMBER)
        self.typed(b"*")
        self.c_string(objc)
        self.typed(objc)
        if objc == b"c":
            if not 0 <= number.value <= 0xFF:
                raise ValueError("a 'c' NSNumber is one raw byte")
            self.out += bytes([number.value])
        else:
            self.out += encode_sint(number.value)
        self.end_object()

    def url_object(self, url: URL) -> None:
        if self.shared(("url", url.relative), enabled=self.share_values):
            return
        self.begin_object(_CHAIN_URL)
        self.typed(b"c")
        self.out += b"\x00"
        self.typed(b"@")
        self.string_object(url.relative)
        self.end_object()

    def data_object(self, data: Data) -> None:
        if self.shared(("data", data.mutable, data.payload), enabled=self.share_values):
            return
        self.begin_object(_CHAIN_MUTABLE_DATA if data.mutable else _CHAIN_DATA)
        self.typed(b"i")
        self.out += encode_sint(len(data.payload))
        self.typed(b"[%dc]" % len(data.payload))
        self.out += data.payload
        self.end_object()

    def dictionary_object(self, attributes: Mapping[str, object]) -> None:
        self.begin_object(_CHAIN_DICTIONARY)
        self.typed(b"i")
        self.out += encode_sint(len(attributes))
        for key, value in attributes.items():
            self.typed(b"@")
            self.string_object(key, key=True)
            self.typed(b"@")
            self.value_object(value)
        self.end_object()

    def value_object(self, value: object) -> None:
        if isinstance(value, bool):
            self.number_object(Number(int(value), "c"))
        elif isinstance(value, int):
            self.number_object(Number(value, "i"))
        elif isinstance(value, Number):
            self.number_object(value)
        elif isinstance(value, str):
            self.string_object(value)
        elif isinstance(value, bytes):
            self.data_object(Data(value))
        elif isinstance(value, Data):
            self.data_object(value)
        elif isinstance(value, URL):
            self.url_object(value)
        elif isinstance(value, Mapping):
            self.dictionary_object(value)
        else:
            raise TypeError(f"unsupported attribute value {type(value).__name__}")


def encode_attributed_body_runs(
    text: str,
    runs: list[tuple[int, Mapping[str, object]]],
    *,
    mutable: bool = False,
    share_keys: bool = False,
    share_values: bool = True,
) -> bytes:
    """Build a byte-exact multi-run ``attributedBody`` blob.

    ``runs`` is a list of ``(utf16_length, attributes)`` covering ``text`` in
    order; the lengths must sum to the UTF-16 code-unit length of ``text``
    (``ValueError`` otherwise).  Attribute values: ``int`` -> ``NSNumber`` of
    type ``i``; ``bool`` -> ``NSNumber`` of type ``c``; :class:`Number` for an
    explicit type (``q`` is what the archiver uses for
    ``__kIMBaseWritingDirectionAttributeName``); ``str`` -> ``NSString``;
    ``bytes`` / :class:`Data` -> ``NSData`` or ``NSMutableData``; :class:`URL`
    -> ``NSURL``; a mapping -> a nested ``NSDictionary``.  Keys are written in
    the mapping's iteration order (the archiver writes ``NSDictionary``
    enumeration order, which is not predictable; the reader must not rely on
    it).

    Each distinct attribute dictionary (by equality) is archived once, on the
    first run that uses it; later runs with an equal dictionary carry only its
    1-based index.  Repeated class names and type strings are always written
    as shared-string references, as the archiver does.  Repeated *objects* are
    where the archiver goes by pointer, which bytes cannot show, so two flags
    choose the approximation: ``share_values`` (default True) writes an equal
    value object (``NSNumber``, ``NSString`` value, ``NSURL``, ``NSData``) as an
    object reference to its first archived copy, and ``share_keys`` (default
    False) does the same for an attribute key string.  The defaults reproduce
    19,008 of 20,782 live blobs byte-for-byte; ``share_keys=True`` the 770
    whose key strings were the same constant (docs/TYPEDSTREAM.md section 7).

    ``mutable`` roots the archive at ``NSMutableAttributedString`` with an
    ``NSMutableString`` (the ``MUTABLE_ROOT_SKELETON`` prefix).
    """
    units = len(text.encode("utf-16-le")) // 2
    if sum(length for length, _ in runs) != units:
        raise ValueError(
            f"run lengths sum to {sum(length for length, _ in runs)}, "
            f"text is {units} UTF-16 code units"
        )
    if any(length < 0 for length, _ in runs):
        raise ValueError("run lengths must be non-negative")
    a = _Archiver(share_keys=share_keys, share_values=share_values)
    a.out += b"\x04\x0bstreamtyped" + encode_int(1000)
    a.typed(b"@")
    a.begin_object(_CHAIN_MUTABLE_ATTRIBUTED if mutable else _CHAIN_ATTRIBUTED)
    a.typed(b"@")
    a.string_object(text, mutable=mutable)
    seen: list[Mapping[str, object]] = []
    for length, attributes in runs:
        index = next((i for i, d in enumerate(seen) if d == attributes), None)
        a.typed(b"iI")
        if index is None:
            seen.append(attributes)
            a.out += encode_sint(len(seen)) + encode_int(length)
            a.typed(b"@")
            a.dictionary_object(attributes)
        else:
            a.out += encode_sint(index + 1) + encode_int(length)
    a.end_object()
    return bytes(a.out)
