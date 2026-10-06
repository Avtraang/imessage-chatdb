"""Fuzz tests for the decoders (DESIGN.md 8.4 fuzz; T9).

Three seeded generators, 2,000 iterations each, feed ``extract_text``,
``try_parse_attributed_body``, ``parse_link_preview`` and ``embedded_image``:

1. random bytes of random length;
2. bit-flipped copies of *valid* blobs (typedstream bodies from
   ``typedstream_writer``, keyed archives from ``keyed_archive_writer``);
3. binary plists whose UIDs point anywhere (including out of range, at
   themselves and at each other), with random key names, blobs and depths.

The only acceptable outcomes are a value of the documented type or ``None``;
an exception of any kind is a failure.  Only synthetic text and
``*.example.invalid`` URLs are ever encoded.
"""

from __future__ import annotations

import plistlib
import random
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from imessage_chatdb.link_preview import LinkPreview, embedded_image, parse_link_preview
from imessage_chatdb.typedstream import extract_text
from imessage_chatdb.typedstream_reader import AttributedBody, try_parse_attributed_body
from tests.fixtures.keyed_archive_writer import (
    build_link_archive,
    dump_archive,
    make_image_blob,
    make_link_payload,
)
from tests.fixtures.typedstream_writer import encode_attributed_body

ITERATIONS = 2_000
SEED = 0x1A2B3C

Decoder = Callable[[bytes | None], object]

DECODERS: dict[str, tuple[Decoder, type | tuple[type, ...]]] = {
    "extract_text": (extract_text, str),
    "try_parse_attributed_body": (try_parse_attributed_body, AttributedBody),
    "parse_link_preview": (parse_link_preview, LinkPreview),
    "embedded_image": (embedded_image, bytes),
}


def _check(name: str, data: bytes | None) -> None:
    """Call the decoder; the result must be ``None`` or an instance of its type."""
    fn, expected = DECODERS[name]
    try:
        result = fn(data)
    except Exception as exc:  # pragma: no cover - this is the failure we hunt
        preview = None if data is None else data[:24].hex()
        raise AssertionError(
            f"{name} raised {exc.__class__.__name__} on {len(data or b'')} bytes ({preview}...)"
        ) from exc
    assert result is None or isinstance(result, expected), (name, type(result))
    if isinstance(result, str):
        assert result != ""  # '' is never returned, only None
    if isinstance(result, bytes):
        assert len(result) > 3000


# ---------------------------------------------------------------------------
# corpora of valid inputs
# ---------------------------------------------------------------------------

TEXTS = (
    "",
    "a",
    "hello fixture",
    "x" * 127,
    "y" * 128,
    "emoji \U0001f600 \U0001f1fa\U0001f1f8",
    "שלום עולם",
    "line\nbreak\ttab ￼ placeholder",
    "z" * 70_000,
)


def valid_typedstreams() -> list[bytes]:
    out: list[bytes] = []
    for text in TEXTS:
        for mutable in (False, True):
            out.append(encode_attributed_body(text, mutable=mutable))
    out.append(encode_attributed_body("forced wide", force_len_tag=0x81))
    out.append(encode_attributed_body("forced wider", force_len_tag=0x82))
    out.append(encode_attributed_body("forced widest", force_len_tag=0x83))
    return out


def valid_archives() -> list[bytes]:
    u = "https://example.invalid/page"
    return [
        make_link_payload(u, "Title"),
        make_link_payload(u, "Title", "Summary", "Site", wrapped=True),
        make_link_payload(u, "Title", embed="png"),
        make_link_payload(u, "Title", embed=[("jpeg", 4000), ("heic", 5000), ("raw", 6000)]),
        make_link_payload(u, "Title", image_meta_url="https://img.example.invalid/a.png"),
        make_link_payload(
            u, None, extra_strings=("https://img.example.invalid/hero.jpg", "favicon.ico")
        ),
        make_link_payload(None, "title only"),
        make_link_payload(u, "T", original_url="https://example.invalid/orig"),
        make_link_payload(u, "T", plain_url=True),
        make_link_payload(u, "T", cyclic=True),
        make_link_payload(u, "T", embed=("png", 2000)),
    ]


# ---------------------------------------------------------------------------
# generators
# ---------------------------------------------------------------------------


def random_bytes(rng: random.Random) -> Iterator[bytes]:
    while True:
        n = rng.choice((0, 1, 2, 7, 16, 64, 256, 1024, 4096, rng.randrange(0, 20_000)))
        kind = rng.random()
        if kind < 0.6:
            yield rng.randbytes(n)
        elif kind < 0.8:
            # Random bytes seeded with the magic the decoders look for.
            head = rng.choice(
                (
                    b"\x04\x0bstreamtyped",
                    b"NSString",
                    b"NSString\x01\x94\x84\x01+",
                    b"bplist00",
                    b"\x89PNG\r\n\x1a\n",
                    b"\xff\xd8\xff",
                    b"<?xml",
                    b"<plist",
                )
            )
            yield head + rng.randbytes(n)
        else:
            # Repeated bytes / long runs of one tag.
            yield bytes([rng.randrange(256)]) * n


def flipped(rng: random.Random, corpus: list[bytes]) -> Iterator[bytes]:
    while True:
        blob = bytearray(rng.choice(corpus))
        action = rng.random()
        if action < 0.7:
            for _ in range(rng.randint(1, 8)):
                i = rng.randrange(len(blob))
                blob[i] ^= 1 << rng.randrange(8)
        elif action < 0.85:
            # Truncate at a random point (every prefix must be safe).
            del blob[rng.randrange(len(blob) + 1) :]
        else:
            # Overwrite a random window with random bytes.
            i = rng.randrange(len(blob))
            w = rng.randint(1, 16)
            blob[i : i + w] = rng.randbytes(w)
        yield bytes(blob)


_KEYS = (
    "originalURL",
    "URL",
    "title",
    "summary",
    "siteName",
    "imageMetadata",
    "richLinkMetadata",
    "image",
    "NS.relative",
    "NS.base",
    "$class",
    "$classname",
    "$classes",
    "root",
    "junk",
)


def _random_scalar(rng: random.Random, n_objects: int) -> Any:
    kind = rng.random()
    if kind < 0.35:
        return plistlib.UID(rng.randrange(0, n_objects + 8))
    if kind < 0.5:
        return rng.choice(
            (
                "https://example.invalid/x",
                "http://img.example.invalid/hero.png",
                "https://example.invalid/x?q=1",
                "favicon.ico",
                "",
                "$null",
                "not a url",
            )
        )
    if kind < 0.6:
        return rng.randrange(-5, 1 << 40)
    if kind < 0.7:
        size = rng.choice((0, 1, 2999, 3000, 3001, 3100, 8000))
        return make_image_blob(rng.choice(("png", "jpeg", "heic", "raw")), size)
    if kind < 0.75:
        return rng.random()
    if kind < 0.8:
        return rng.choice((True, False))
    if kind < 0.9:
        return [plistlib.UID(rng.randrange(0, n_objects + 8)) for _ in range(rng.randint(0, 4))]
    return {rng.choice(_KEYS): plistlib.UID(rng.randrange(0, n_objects + 8))}


def random_uid_plists(rng: random.Random) -> Iterator[bytes]:
    base = valid_archives()
    while True:
        mode = rng.random()
        if mode < 0.5:
            # Start from a valid archive dict and rewrite UIDs at random.
            plist = build_link_archive(
                "https://example.invalid/p",
                "T",
                wrapped=rng.random() < 0.5,
                embed="png" if rng.random() < 0.3 else None,
                image_meta_url="https://img.example.invalid/i.png" if rng.random() < 0.3 else None,
            )
            objects: list[Any] = plist["$objects"]
            n = len(objects)
            for _ in range(rng.randint(1, 6)):
                i = rng.randrange(n)
                obj = objects[i]
                if isinstance(obj, dict) and obj:
                    key = rng.choice(list(obj))
                    obj[key] = _random_scalar(rng, n)
                else:
                    objects[i] = _random_scalar(rng, n)
            if rng.random() < 0.3:
                plist["$top"] = {"root": plistlib.UID(rng.randrange(0, n + 8))}
            if rng.random() < 0.1:
                plist["$top"] = rng.choice(({}, {"root": "string"}, {"root": [1, 2]}))
            if rng.random() < 0.05:
                del plist["$objects"]
            yield dump_archive(plist)
        elif mode < 0.9:
            # Build a fresh object table of random shape.
            n = rng.randint(1, 12)
            objects = ["$null"] if rng.random() < 0.8 else []
            for _ in range(n):
                if rng.random() < 0.6:
                    width = rng.randint(0, 4)
                    objects.append(
                        {rng.choice(_KEYS): _random_scalar(rng, n + 1) for _ in range(width)}
                    )
                else:
                    objects.append(_random_scalar(rng, n + 1))
            top: Any = {"root": plistlib.UID(rng.randrange(0, n + 8))}
            if rng.random() < 0.1:
                top = plistlib.UID(rng.randrange(0, n + 8))
            plist = {
                "$archiver": "NSKeyedArchiver",
                "$version": 100000,
                "$top": top,
                "$objects": objects,
            }
            fmt = plistlib.FMT_BINARY if rng.random() < 0.8 else plistlib.FMT_XML
            try:
                yield plistlib.dumps(plist, fmt=fmt)
            except (TypeError, ValueError, OverflowError):
                # XML plists cannot carry UIDs / negative ints; try binary.
                yield plistlib.dumps(plist, fmt=plistlib.FMT_BINARY)
        else:
            # Non-dict / odd top-level plists.
            weird: Any = rng.choice(
                (
                    [],
                    [plistlib.UID(3), plistlib.UID(0)],
                    "just a string",
                    {"$objects": "not a list", "$top": {"root": plistlib.UID(1)}},
                    {"$objects": [], "$top": {"root": plistlib.UID(0)}},
                    {
                        "$objects": [plistlib.UID(1), plistlib.UID(0)],
                        "$top": {"root": plistlib.UID(1)},
                    },
                    {"$objects": [make_image_blob("png", 5000)]},
                    {"$objects": [], "$top": "nope"},
                )
            )
            yield plistlib.dumps(weird, fmt=plistlib.FMT_BINARY)
        if rng.random() < 0.02:
            yield rng.choice(base)


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(DECODERS))
def test_random_bytes_never_raise(name: str) -> None:
    rng = random.Random(SEED + 1)
    gen = random_bytes(rng)
    for _ in range(ITERATIONS):
        _check(name, next(gen))
    _check(name, None)
    _check(name, b"")


@pytest.mark.parametrize("name", sorted(DECODERS))
def test_bit_flipped_typedstreams_never_raise(name: str) -> None:
    rng = random.Random(SEED + 2)
    gen = flipped(rng, valid_typedstreams())
    for _ in range(ITERATIONS):
        _check(name, next(gen))


@pytest.mark.parametrize("name", sorted(DECODERS))
def test_bit_flipped_archives_never_raise(name: str) -> None:
    rng = random.Random(SEED + 3)
    gen = flipped(rng, valid_archives())
    for _ in range(ITERATIONS):
        _check(name, next(gen))


@pytest.mark.parametrize("name", sorted(DECODERS))
def test_random_uid_plists_never_raise(name: str) -> None:
    rng = random.Random(SEED + 4)
    gen = random_uid_plists(rng)
    for _ in range(ITERATIONS):
        _check(name, next(gen))


def test_corpora_are_valid() -> None:
    """The fuzz corpora really are decodable, so the flips start from good inputs."""
    for blob in valid_typedstreams():
        text = extract_text(blob)
        assert text is None or isinstance(text, str)
    assert extract_text(encode_attributed_body("hello fixture")) == "hello fixture"
    assert sum(1 for a in valid_archives() if parse_link_preview(a) is not None) >= 8
    assert embedded_image(make_link_payload("https://example.invalid/", "T", embed="png"))


def test_generators_are_deterministic() -> None:
    a = [next(g) for g in [random_uid_plists(random.Random(7))] for _ in range(20)]
    b = [next(g) for g in [random_uid_plists(random.Random(7))] for _ in range(20)]
    assert a == b
