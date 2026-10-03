"""Pins for ``keyed_archive`` and ``link_preview`` (DESIGN.md sections 6.3 and 8.3)."""

from __future__ import annotations

import dataclasses
import plistlib
from typing import Any

import pytest

from imessage_chatdb.keyed_archive import KeyedArchive
from imessage_chatdb.link_preview import (
    LINK_BALLOON,
    SKIP_IMG,
    LinkPreview,
    embedded_image,
    embedded_images,
    parse_link_preview,
    sniff_image_mime,
)
from tests.fixtures.keyed_archive_writer import (
    build_link_archive,
    dump_archive,
    make_image_blob,
    make_link_payload,
)

PAGE = "https://www.example.invalid/articles/42"
HERO = "https://cdn.example.invalid/hero.jpg"


# --------------------------------------------------------------------------- constants


def test_constants_match_relay() -> None:
    assert LINK_BALLOON == "com.apple.messages.URLBalloonProvider"
    assert SKIP_IMG == ("favicon", "apple-touch", ".ico")
    assert isinstance(SKIP_IMG, tuple)


def test_link_preview_is_frozen_and_slotted() -> None:
    lp = parse_link_preview(make_link_payload(PAGE, "T"))
    assert lp is not None
    assert dataclasses.is_dataclass(LinkPreview)
    assert hasattr(LinkPreview, "__slots__")
    assert not hasattr(lp, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        lp.title = "x"  # type: ignore[misc]
    assert [f.name for f in dataclasses.fields(LinkPreview)] == [
        "url",
        "title",
        "summary",
        "site",
        "image_url",
        "has_embedded_image",
    ]


# --------------------------------------------------------------------------- fields


def test_flat_equals_wrapped() -> None:
    kw: dict[str, Any] = dict(
        url=PAGE, title="Title", summary="Summary", site="Example", image_meta_url=HERO
    )
    flat = parse_link_preview(make_link_payload(**kw))
    wrapped = parse_link_preview(make_link_payload(wrapped=True, **kw))
    assert flat is not None
    assert flat == wrapped
    assert flat == LinkPreview(
        url=PAGE,
        title="Title",
        summary="Summary",
        site="Example",
        image_url=HERO,
        has_embedded_image=False,
    )


def test_original_url_beats_url() -> None:
    orig = "https://example.invalid/original"
    lp = parse_link_preview(make_link_payload(PAGE, "T", original_url=orig))
    assert lp is not None
    assert lp.url == orig


def test_url_from_archived_nsurl_and_from_plain_string() -> None:
    archived = parse_link_preview(make_link_payload(PAGE, "T"))
    plain = parse_link_preview(make_link_payload(PAGE, "T", plain_url=True))
    assert archived is not None and plain is not None
    assert archived.url == PAGE
    assert plain.url == PAGE
    # The archived form really is an NSURL dict with NS.relative in the table.
    archive = KeyedArchive.load(make_link_payload(PAGE, "T"))
    assert isinstance(archive.root, dict)
    nsurl = archive.deref(archive.root["URL"])
    assert isinstance(nsurl, dict) and "NS.relative" in nsurl


def test_url_only_and_title_only_accepted_neither_rejected() -> None:
    url_only = parse_link_preview(make_link_payload(url=PAGE))
    assert url_only is not None
    assert (url_only.url, url_only.title) == (PAGE, None)

    title_only = parse_link_preview(make_link_payload(title="Just a title"))
    assert title_only is not None
    assert (title_only.url, title_only.title) == (None, "Just a title")

    # Summary and site alone are not enough.
    assert parse_link_preview(make_link_payload(summary="s", site="Example")) is None
    assert parse_link_preview(make_link_payload()) is None


def test_empty_strings_are_treated_as_missing() -> None:
    assert parse_link_preview(make_link_payload(url="", title="")) is None
    lp = parse_link_preview(make_link_payload(url="", title="T", summary="", site=""))
    assert lp is not None
    assert (lp.url, lp.summary, lp.site) == (None, None, None)


def test_summary_and_site_default_to_none() -> None:
    lp = parse_link_preview(make_link_payload(PAGE))
    assert lp is not None
    assert lp.summary is None and lp.site is None and lp.image_url is None
    assert lp.has_embedded_image is False


# --------------------------------------------------------------------------- embedded image


@pytest.mark.parametrize(
    ("kind", "size", "expected"),
    [
        ("png", 3001, True),
        ("jpeg", 3001, True),
        ("heic", 3001, True),
        ("png", 3000, False),  # threshold is strictly greater than 3000
        ("jpeg", 2000, False),  # the 2,000-byte decoy
        ("raw", 4000, False),  # big, but no magic head
        ("png", 3100, True),
    ],
)
def test_has_embedded_image_threshold_and_magic(kind: str, size: int, expected: bool) -> None:
    lp = parse_link_preview(make_link_payload(PAGE, "T", embed=(kind, size)))
    assert lp is not None
    assert lp.has_embedded_image is expected
    blob = embedded_image(make_link_payload(PAGE, "T", embed=(kind, size)))
    if expected:
        assert blob == make_image_blob(kind, size)
    else:
        assert blob is None


def test_embedded_image_returns_largest_blob() -> None:
    payload = make_link_payload(
        PAGE, "T", embed=[("png", 3500), ("jpeg", 9000), ("heic", 4200), ("raw", 20000)]
    )
    assert embedded_image(payload) == make_image_blob("jpeg", 9000)
    lp = parse_link_preview(payload)
    assert lp is not None and lp.has_embedded_image is True


def test_embedded_image_without_payload_or_image() -> None:
    assert embedded_image(None) is None
    assert embedded_image(b"") is None
    assert embedded_image(b"\x00not a plist") is None
    assert embedded_image(make_link_payload(PAGE, "T")) is None
    assert embedded_image(make_link_payload(PAGE, "T", embed=("png", 2000))) is None
    # A plist that is a list, or a dict without $objects, yields None quietly.
    assert embedded_image(plistlib.dumps([1, 2], fmt=plistlib.FMT_BINARY)) is None
    assert embedded_image(plistlib.dumps({"x": 1}, fmt=plistlib.FMT_BINARY)) is None
    assert embedded_image(plistlib.dumps({"$objects": "nope"}, fmt=plistlib.FMT_BINARY)) is None


def test_embedded_image_needs_only_the_objects_table() -> None:
    """Image extraction reads $objects alone; parse_link_preview needs $top too."""
    blob = make_image_blob("png", 5000)
    payload = plistlib.dumps({"$objects": ["$null", blob]}, fmt=plistlib.FMT_BINARY)
    assert embedded_image(payload) == blob
    assert embedded_images(payload) == [blob]
    assert parse_link_preview(payload) is None
    with pytest.raises(ValueError):
        KeyedArchive.load(payload)  # the stricter loader rejects a missing $top


# --------------------------------------------------------------------------- embedded_images


def test_embedded_images_distinguishes_unparseable_from_no_image() -> None:
    """None = unusable payload; [] = well-formed archive without a blob (DESIGN.md 7.3)."""
    # unusable -> None
    assert embedded_images(None) is None
    assert embedded_images(b"") is None
    assert embedded_images(b"garbage") is None
    assert embedded_images(b"\x00not a plist") is None
    assert embedded_images(plistlib.dumps([1, 2], fmt=plistlib.FMT_BINARY)) is None
    assert embedded_images(plistlib.dumps({"x": 1}, fmt=plistlib.FMT_BINARY)) is None
    assert embedded_images(plistlib.dumps({"$objects": "nope"}, fmt=plistlib.FMT_BINARY)) is None
    # parsed, nothing qualifies -> []
    assert embedded_images(make_link_payload(PAGE, "T")) == []
    assert embedded_images(make_link_payload(PAGE, "T", embed=("png", 2000))) == []
    assert embedded_images(plistlib.dumps({"$objects": []}, fmt=plistlib.FMT_BINARY)) == []


def test_embedded_images_table_order_and_largest_first_on_tie() -> None:
    small = make_image_blob("png", 3500)
    big_a = make_image_blob("jpeg", 9000)
    big_b = make_image_blob("heic", 9000)
    payload = make_link_payload(PAGE, "T", embed=[("png", 3500), ("jpeg", 9000), ("heic", 9000)])
    blobs = embedded_images(payload)
    assert blobs == [small, big_a, big_b]
    # The selection rule: largest wins, the first of the largest on a tie.
    assert embedded_image(payload) == big_a
    assert embedded_image(payload) == max(blobs, key=len)


def test_embedded_images_never_raises_on_odd_object_tables() -> None:
    for objects in ([None], [1, 2.5, "s", {"k": b"x" * 4000}, [b"\x89PNG" + b"\0" * 4000]],):
        payload = plistlib.dumps({"$objects": objects}, fmt=plistlib.FMT_BINARY)
        assert embedded_images(payload) == []


# --------------------------------------------------------------------------- image_url


def test_image_url_prefers_image_metadata_over_scan() -> None:
    lp = parse_link_preview(
        make_link_payload(PAGE, "T", image_meta_url=HERO, extra_strings=("https://x.invalid/a",))
    )
    assert lp is not None
    assert lp.image_url == HERO


def test_image_url_is_computed_even_with_embedded_blob() -> None:
    lp = parse_link_preview(make_link_payload(PAGE, "T", image_meta_url=HERO, embed="png"))
    assert lp is not None
    assert lp.has_embedded_image is True
    assert lp.image_url == HERO


def test_image_metadata_url_must_start_with_http() -> None:
    lp = parse_link_preview(
        make_link_payload(
            PAGE, "T", image_meta_url="ftp://files.example.invalid/x.png", extra_strings=(HERO,)
        )
    )
    assert lp is not None
    assert lp.image_url == HERO  # fell through to the scan


def test_scan_skips_page_url_variants_and_icons() -> None:
    page = "https://Example.invalid/Some/Page/?utm_source=x"
    extras = (
        "https://example.invalid/some/page",  # page URL: case, query and slash stripped
        "HTTPS://EXAMPLE.INVALID/some/page/",
        "https://example.invalid/favicon.ico",
        "https://cdn.example.invalid/apple-touch-icon-180.png",
        "https://cdn.example.invalid/img/thing.ICO",
        "not a url at all",
        HERO,
        "https://cdn.example.invalid/second.jpg",
    )
    lp = parse_link_preview(make_link_payload(page, "T", extra_strings=extras))
    assert lp is not None
    assert lp.image_url == HERO


def test_scan_returns_none_when_only_page_and_icons() -> None:
    lp = parse_link_preview(
        make_link_payload(PAGE, "T", extra_strings=(PAGE + "/", "https://a.invalid/favicon.png"))
    )
    assert lp is not None
    assert lp.image_url is None


def test_scan_without_page_url_does_not_skip_anything() -> None:
    lp = parse_link_preview(make_link_payload(title="T", extra_strings=("https://a.invalid/i.jpg",)))
    assert lp is not None
    assert lp.image_url == "https://a.invalid/i.jpg"


# --------------------------------------------------------------------------- robustness


def test_none_and_empty_payload() -> None:
    assert parse_link_preview(None) is None
    assert parse_link_preview(b"") is None


def test_non_plist_bytes() -> None:
    assert parse_link_preview(b"\x00\x01\x02 definitely not a plist") is None
    assert parse_link_preview(b"<?xml version='1.0'?><plist><dict>") is None
    assert parse_link_preview(b"bplist00garbage") is None


def test_plist_without_archive_shape() -> None:
    assert parse_link_preview(plistlib.dumps([1, 2, 3], fmt=plistlib.FMT_BINARY)) is None
    assert parse_link_preview(plistlib.dumps({"a": 1}, fmt=plistlib.FMT_BINARY)) is None
    assert parse_link_preview(plistlib.dumps({"$objects": []}, fmt=plistlib.FMT_BINARY)) is None
    no_root = plistlib.dumps({"$objects": ["$null"], "$top": {}}, fmt=plistlib.FMT_BINARY)
    assert parse_link_preview(no_root) is None


def test_cyclic_archive_returns_none_without_recursion_error() -> None:
    assert parse_link_preview(make_link_payload(PAGE, "T", cyclic=True)) is None
    # A two-node cycle at the root.
    arch = build_link_archive(PAGE, "T")
    objs = arch["$objects"]
    a = len(objs)
    objs.append(plistlib.UID(a + 1))
    objs.append(plistlib.UID(a))
    arch["$top"]["root"] = plistlib.UID(a)
    assert parse_link_preview(dump_archive(arch)) is None
    # A cycle on a field leaves the rest intact.
    arch = build_link_archive(PAGE, "T")
    objs = arch["$objects"]
    k = len(objs)
    objs.append(plistlib.UID(k))
    objs[1]["summary"] = plistlib.UID(k)
    lp = parse_link_preview(dump_archive(arch))
    assert lp is not None
    assert lp.summary is None and lp.url == PAGE


def test_string_root_returns_none() -> None:
    arch = build_link_archive(PAGE, "T")
    idx = arch["$objects"].index(PAGE)
    arch["$top"]["root"] = plistlib.UID(idx)
    assert parse_link_preview(dump_archive(arch)) is None


def test_out_of_range_uid() -> None:
    arch = build_link_archive(PAGE, "T")
    arch["$top"]["root"] = plistlib.UID(10_000)
    assert parse_link_preview(dump_archive(arch)) is None

    arch = build_link_archive(PAGE, "T")
    arch["$objects"][1]["title"] = plistlib.UID(10_000)
    arch["$objects"][1]["imageMetadata"] = plistlib.UID(10_000)
    lp = parse_link_preview(dump_archive(arch))
    assert lp is not None
    assert lp.title is None and lp.url == PAGE and lp.image_url is None


def test_rich_link_wrapper_pointing_at_non_dict_is_ignored() -> None:
    arch = build_link_archive(PAGE, "T")
    idx = arch["$objects"].index("T")
    arch["$objects"][1]["richLinkMetadata"] = plistlib.UID(idx)
    lp = parse_link_preview(dump_archive(arch))
    assert lp is not None and lp.url == PAGE


# --------------------------------------------------------------------------- sniff


@pytest.mark.parametrize(
    ("head", "mime"),
    [
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "image/png"),
        (b"\xff\xd8\xff\xe0\x00\x10JFIF", "image/jpeg"),
        (b"\x00\x00\x00\x18ftypheic\x00\x00", "image/heic"),
        (b"\x00\x00\x00\x1cftypavif", "image/heic"),  # any ISOBMFF, as the relay labels it
        (b"GIF89a\x01\x00\x01\x00", "image/gif"),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"RIFF\x24\x00\x00\x00WAVEfmt ", "application/octet-stream"),
        (b"%PDF-1.4", "application/octet-stream"),
        (b"", "application/octet-stream"),
        (b"\x89", "application/octet-stream"),
    ],
)
def test_sniff_image_mime(head: bytes, mime: str) -> None:
    assert sniff_image_mime(head) == mime


def test_sniff_on_embedded_blobs() -> None:
    for kind, mime in (("png", "image/png"), ("jpeg", "image/jpeg"), ("heic", "image/heic")):
        blob = embedded_image(make_link_payload(PAGE, "T", embed=kind))
        assert blob is not None
        assert sniff_image_mime(blob[:12]) == mime


# --------------------------------------------------------------------------- KeyedArchive


def test_load_validates_shape() -> None:
    with pytest.raises(ValueError):
        KeyedArchive.load(b"not a plist")
    with pytest.raises(ValueError):
        KeyedArchive.load(plistlib.dumps([1], fmt=plistlib.FMT_BINARY))
    with pytest.raises(ValueError):
        KeyedArchive.load(plistlib.dumps({"$top": {"root": 1}}, fmt=plistlib.FMT_BINARY))
    with pytest.raises(ValueError):
        KeyedArchive.load(plistlib.dumps({"$objects": ["$null"]}, fmt=plistlib.FMT_BINARY))
    with pytest.raises(ValueError):
        KeyedArchive.load(plistlib.dumps({"$objects": {}, "$top": {}}, fmt=plistlib.FMT_BINARY))
    archive = KeyedArchive.load(make_link_payload(PAGE, "T"))
    assert isinstance(archive.root, dict)
    assert archive.objects[0] == "$null"
    assert archive.root is archive.objects[1]


def test_load_tolerates_missing_root() -> None:
    archive = KeyedArchive.load(
        plistlib.dumps({"$objects": ["$null"], "$top": {}}, fmt=plistlib.FMT_BINARY)
    )
    assert archive.root is None


def test_deref_chain_out_of_range_and_depth() -> None:
    objs: list[object] = ["$null", plistlib.UID(2), plistlib.UID(3), "end"]
    a = KeyedArchive(objs, plistlib.UID(1))
    assert a.root == "end"
    assert a.deref(plistlib.UID(1)) == "end"
    assert a.deref(plistlib.UID(3)) == "end"
    assert a.deref("plain") == "plain"
    assert a.deref(None) is None
    assert a.deref(plistlib.UID(4)) is None
    assert a.deref(plistlib.UID(2**40)) is None
    # Three hops are needed from UID(1); a cap of 2 refuses, a cap of 3 allows.
    assert a.deref(plistlib.UID(1), max_depth=2) is None
    assert a.deref(plistlib.UID(1), max_depth=3) == "end"


def test_deref_cycle_uses_visited_set() -> None:
    objs: list[object] = ["$null", plistlib.UID(2), plistlib.UID(1), plistlib.UID(3)]
    a = KeyedArchive(objs, plistlib.UID(1))
    assert a.root is None
    assert a.deref(plistlib.UID(3)) is None
    assert a.deref(plistlib.UID(1), max_depth=10**6) is None  # visited set, not the depth cap


def test_string_and_nested_url_helpers() -> None:
    archive = KeyedArchive.load(
        make_link_payload(PAGE, "T", original_url="https://o.invalid/", image_meta_url=HERO)
    )
    root = archive.root
    assert isinstance(root, dict)
    assert archive.string(root, "originalURL", "URL") == "https://o.invalid/"
    assert archive.string(root, "URL") == PAGE
    assert archive.string(root, "missing", "title") == "T"
    assert archive.string(root, "missing") is None
    assert archive.string(root) is None
    assert archive.nested_url(root, "imageMetadata") == HERO
    assert archive.nested_url(root, "title") is None  # a string, not a dict
    assert archive.nested_url(root, "nope") is None
    assert archive.nsurl(root["URL"]) == PAGE
    assert archive.nsurl(root["title"]) == "T"
    assert archive.nsurl(plistlib.UID(0)) == "$null"  # verbatim relay behaviour
    assert archive.nsurl(42) is None
    assert archive.nsurl({"NS.relative": ""}) is None


# --------------------------------------------------------------------------- relay parity


def _relay_deref(objs: list[Any], v: Any, depth: int = 0) -> Any:
    """Verbatim copy of the relay's UID dereference (depth cap 8, no visited set)."""
    if depth > 8:
        return None
    if isinstance(v, plistlib.UID):
        i = v.data
        return _relay_deref(objs, objs[i], depth + 1) if 0 <= i < len(objs) else None
    return v


def _relay_parse_link_preview(payload: bytes | None, rowid: int) -> dict[str, Any] | None:
    """Verbatim copy of the relay's link-preview parser (the oracle for section 6.3)."""
    if not payload:
        return None
    try:
        plist = plistlib.loads(payload)
        objs = plist["$objects"]
        root = _relay_deref(objs, plist["$top"]["root"])
    except Exception:
        return None
    if not isinstance(root, dict):
        return None
    inner = _relay_deref(objs, root.get("richLinkMetadata"))
    if isinstance(inner, dict):
        root = inner

    def field(*names: str) -> str | None:
        for n in names:
            v = _relay_deref(objs, root.get(n))
            if isinstance(v, str) and v:
                return v
            if isinstance(v, dict):
                rel = _relay_deref(objs, v.get("NS.relative"))
                if isinstance(rel, str) and rel:
                    return rel
        return None

    url = field("originalURL", "URL")
    title = field("title")
    if not url and not title:
        return None

    blob = None
    for o in objs:
        if isinstance(o, bytes) and len(o) > 3000:
            if o[:4] == b"\x89PNG" or o[:2] == b"\xff\xd8" or o[4:8] == b"ftyp":
                if blob is None or len(o) > len(blob):
                    blob = o
    image = f"/link_image/{rowid}" if blob else None

    def meta_url(key: str) -> str | None:
        node = _relay_deref(objs, root.get(key))
        if not isinstance(node, dict):
            return None
        u = _relay_deref(objs, node.get("URL"))
        if isinstance(u, dict):
            u = _relay_deref(objs, u.get("NS.relative"))
        return u if isinstance(u, str) and u.startswith("http") else None

    if not image:
        image = meta_url("imageMetadata")

    if not image:
        page = {u.split("?")[0].rstrip("/").lower() for u in (url,) if u}
        cands = []
        for o in objs:
            if not isinstance(o, str) or not o.startswith("http"):
                continue
            low = o.lower()
            if low.split("?")[0].rstrip("/") in page:
                continue
            if any(x in low for x in SKIP_IMG):
                continue
            cands.append(o)
        image = cands[0] if cands else None

    return {
        "url": url,
        "title": title,
        "summary": field("summary"),
        "site": field("siteName"),
        "image": image,
    }


def _adapter(lp: LinkPreview | None, rowid: int) -> dict[str, Any] | None:
    """The relay adapter from DESIGN.md section 7.2 (``relay_link``)."""
    if lp is None:
        return None
    return {
        "url": lp.url,
        "title": lp.title,
        "summary": lp.summary,
        "site": lp.site,
        "image": f"/link_image/{rowid}" if lp.has_embedded_image else lp.image_url,
    }


PARITY_CASES: list[dict[str, Any]] = [
    {},
    {"url": PAGE},
    {"title": "only"},
    {"summary": "s", "site": "Example"},
    {"url": PAGE, "title": "T", "summary": "S", "site": "Example"},
    {"url": PAGE, "title": "T", "wrapped": True},
    {"url": PAGE, "title": "T", "original_url": "https://o.invalid/x"},
    {"url": PAGE, "title": "T", "plain_url": True},
    {"url": PAGE, "title": "T", "embed": "png"},
    {"url": PAGE, "title": "T", "embed": ("jpeg", 3000)},
    {"url": PAGE, "title": "T", "embed": [("png", 3500), ("heic", 7000)], "image_meta_url": HERO},
    {"url": PAGE, "title": "T", "image_meta_url": HERO, "extra_strings": ("https://z.invalid/",)},
    {"url": PAGE, "title": "T", "image_meta_url": "ftp://no.invalid/", "extra_strings": (HERO,)},
    {
        "url": "https://Example.invalid/P/?q=1",
        "title": "T",
        "extra_strings": (
            "https://example.invalid/p",
            "https://example.invalid/favicon.ico",
            "https://cdn.example.invalid/apple-touch-icon.png",
            HERO,
        ),
    },
    {"url": PAGE, "title": "T", "wrapped": True, "embed": "heic", "extra_strings": (HERO,)},
    {"url": PAGE, "title": "T", "cyclic": True},
    {"url": "", "title": ""},
    {"url": "", "title": "T", "summary": "", "site": ""},
]


@pytest.mark.parametrize("kw", PARITY_CASES, ids=[str(i) for i in range(len(PARITY_CASES))])
def test_parity_with_relay_parse_link_preview(kw: dict[str, Any]) -> None:
    payload = make_link_payload(**kw)
    assert _adapter(parse_link_preview(payload), 77) == _relay_parse_link_preview(payload, 77)


def test_parity_on_hand_built_shapes() -> None:
    shapes: list[bytes] = [
        b"",
        b"garbage",
        plistlib.dumps([1], fmt=plistlib.FMT_BINARY),
        plistlib.dumps({"$objects": ["$null"], "$top": {}}, fmt=plistlib.FMT_BINARY),
    ]
    arch = build_link_archive(PAGE, "T")
    arch["$top"]["root"] = plistlib.UID(arch["$objects"].index(PAGE))
    shapes.append(dump_archive(arch))
    arch = build_link_archive(PAGE, "T")
    arch["$top"]["root"] = plistlib.UID(999)
    shapes.append(dump_archive(arch))
    for payload in shapes:
        assert _adapter(parse_link_preview(payload), 5) == _relay_parse_link_preview(payload, 5)
