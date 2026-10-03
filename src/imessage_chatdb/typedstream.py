"""Text extraction from ``message.attributedBody`` (DESIGN.md sections 4.3 and 6.2).

``attributedBody`` is an NeXT/Apple *typedstream* archive of an
``NSAttributedString``.  v0.1 ships only the byte-scan fast path that the relay
has always used: find ``b"NSString"``, skip to the ``+`` type marker, read the
length tag, decode the UTF-8 bytes.  Two knowing deviations from the relay,
both recorded in ``CHANGELOG.md``:

1. Length-tag handling: ``0x82`` reads **4** bytes (the relay read 3), ``0x83``
   reads 8, and any other tag ``>= 0x80`` yields ``None`` instead of being used
   as a raw length.  No live row uses ``0x82``/``0x83`` (largest text 9,966
   bytes), so the relay's output cannot change today.
2. Truncated blobs: a blob cut before the end of its text -- in the header, in
   the length tag or its length bytes, or inside the text itself -- yields
   ``None``, never a partial string; a blob cut only in the ``0x86`` trailer
   after the text (the text is complete) yields the complete text, exactly as
   an intact blob would.  The relay returned the partial decode.  Every live
   blob carries the trailer after its text, so the ``None`` case is reachable
   only on a corrupt ``attributedBody``.

The three functions never raise.  The full typedstream reader is v0.2 scope and
will use :func:`extract_text` as its oracle on every well-formed blob.
"""

from __future__ import annotations

__all__ = ["extract_text", "effective_text", "clean_text"]

# "NSString" is 8 bytes; the relay skips 6 (landing on "ng") and then scans
# forward for the ``+`` type marker.  Kept verbatim.
_NSSTRING = b"NSString"
_LOCATOR_SKIP = 6
_PLUS = b"\x2b"


def _read_length(chunk: bytes, j: int) -> tuple[int, int] | None:
    """Read a typedstream integer at ``chunk[j]``.

    Returns ``(value, next_index)`` or ``None`` when the tag is one the byte-scan
    does not understand (any tag ``>= 0x80`` other than ``0x81``/``0x82``/``0x83``)
    or when the tag's payload is cut short.
    """
    tag = chunk[j]
    j += 1
    if tag < 0x80:
        return tag, j
    if tag == 0x81:
        width = 2
    elif tag == 0x82:
        width = 4
    elif tag == 0x83:
        width = 8
    else:
        return None
    raw = chunk[j : j + width]
    if len(raw) != width:
        return None
    return int.from_bytes(raw, "little"), j + width


def extract_text(data: bytes | None) -> str | None:
    """Return the text of an ``attributedBody`` blob, or ``None``.

    Locate ``b"NSString"``, skip 6, find ``0x2b``, read the length tag
    (``< 0x80`` one byte | ``0x81`` + u16le | ``0x82`` + u32le | ``0x83`` + u64le |
    any other ``>= 0x80`` -> ``None``), then decode that many bytes as UTF-8 with
    ``errors="replace"``.  An empty result, a blob that is cut before the end of
    the text, and any exception all yield ``None``.  Never raises.
    """
    if not data:
        return None
    try:
        start = data.index(_NSSTRING) + _LOCATOR_SKIP
        chunk = data[start:]
        p = chunk.find(_PLUS)
        if p == -1:
            return None
        read = _read_length(chunk, p + 1)
        if read is None:
            return None
        length, j = read
        body = chunk[j : j + length]
        if len(body) != length:
            return None
        return body.decode("utf-8", errors="replace") or None
    except Exception:
        return None


def effective_text(text_column: str | None, blob: bytes | None) -> str | None:
    """The relay's text rule, exactly.

    ``text_column`` if truthy; else ``extract_text(blob)`` if ``blob`` is truthy;
    else ``text_column`` unchanged (``""`` stays ``""``, ``None`` stays ``None``).
    Note that ``""`` plus a blob whose decode fails yields ``None``, not ``""``.
    """
    if text_column:
        return text_column
    if blob:
        return extract_text(blob)
    return text_column


def clean_text(s: str | None) -> str:
    """Drop U+FFFC (attachment placeholders) and collapse whitespace; ``""`` for ``None``.

    This is the relay's ``" ".join(text.replace("\\ufffc", "").split())`` verbatim.
    """
    if not s:
        return ""
    return " ".join(s.replace("￼", "").split())
