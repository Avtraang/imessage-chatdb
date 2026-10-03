"""Decode the rich-link metadata Messages stores in ``message.payload_data``.

For a URL balloon (``balloon_bundle_id == LINK_BALLOON``) Messages fetches the
page metadata once and archives it as an ``LPLinkMetadata`` keyed archive
(macOS 26 "Tahoe" wraps it in a ``RichLink`` whose ``richLinkMetadata`` holds
the inner object).  Reading that blob gives the URL, title, summary, site name
and hero image without any network access.

The decoding rules are those of the HTTP relay this library was extracted
from (DESIGN.md section 6.3).  Decoders never raise: anything that is not a
usable archive yields ``None``.
"""

from __future__ import annotations

import plistlib
from collections.abc import Iterable
from dataclasses import dataclass

from .keyed_archive import KeyedArchive

__all__ = [
    "LINK_BALLOON",
    "SKIP_IMG",
    "LinkPreview",
    "parse_link_preview",
    "embedded_images",
    "embedded_image",
    "sniff_image_mime",
]

LINK_BALLOON = "com.apple.messages.URLBalloonProvider"
"""``message.balloon_bundle_id`` of a URL (rich link) balloon."""

SKIP_IMG: tuple[str, ...] = ("favicon", "apple-touch", ".ico")
"""Substrings that mark an archived URL as an icon rather than a hero image."""

_EMBEDDED_IMAGE_MIN_BYTES = 3000


@dataclass(frozen=True, slots=True)
class LinkPreview:
    """Facts decoded from a URL balloon's ``payload_data``.

    ``image_url`` is always computed (``imageMetadata`` first, then a scan of
    the archive's strings).  ``has_embedded_image`` says whether the archive
    also carries the hero image bytes; fetch them with :func:`embedded_image`.
    Callers that want "embedded blob first" precedence check
    ``has_embedded_image`` before falling back to ``image_url``.
    """

    url: str | None
    title: str | None
    summary: str | None
    site: str | None
    image_url: str | None
    has_embedded_image: bool


def _is_image_blob(o: object) -> bool:
    return (
        isinstance(o, bytes)
        and len(o) > _EMBEDDED_IMAGE_MIN_BYTES
        and (o[:4] == b"\x89PNG" or o[:2] == b"\xff\xd8" or o[4:8] == b"ftyp")
    )


def _image_blobs(objects: Iterable[object]) -> list[bytes]:
    return [o for o in objects if isinstance(o, bytes) and _is_image_blob(o)]


def _scan_image_url(objects: Iterable[object], page_url: str | None) -> str | None:
    """First ``http`` string in the table that is neither the page URL nor an icon."""
    page = {u.split("?")[0].rstrip("/").lower() for u in (page_url,) if u}
    for o in objects:
        if not isinstance(o, str) or not o.startswith("http"):
            continue
        low = o.lower()
        if low.split("?")[0].rstrip("/") in page:
            continue
        if any(x in low for x in SKIP_IMG):
            continue
        return o
    return None


def _parse(payload: bytes) -> LinkPreview | None:
    archive = KeyedArchive.load(payload)
    root = archive.root
    if not isinstance(root, dict):
        return None
    # Tahoe wraps the metadata: root is a RichLink whose richLinkMetadata holds
    # the LPLinkMetadata.  Older payloads are flat.
    inner = archive.deref(root.get("richLinkMetadata"))
    if isinstance(inner, dict):
        root = inner

    url = archive.string(root, "originalURL", "URL")
    title = archive.string(root, "title")
    if not url and not title:
        return None

    has_embedded_image = bool(_image_blobs(archive.objects))
    # The hero image URL lives in `imageMetadata` (an LPImageMetadata with a
    # real NSURL); `image` itself only carries a MIME type and an attachment
    # index.  Last resort: scan the archive's strings.
    image_url = archive.nested_url(root, "imageMetadata") or _scan_image_url(
        archive.objects, url
    )

    return LinkPreview(
        url=url,
        title=title,
        summary=archive.string(root, "summary"),
        site=archive.string(root, "siteName"),
        image_url=image_url,
        has_embedded_image=has_embedded_image,
    )


def parse_link_preview(payload: bytes | None) -> LinkPreview | None:
    """Decode a URL balloon's ``payload_data``; ``None`` when nothing usable is there.

    Returns ``None`` for an empty payload, bytes that are not a keyed archive,
    a root that is not a dictionary (including cyclic or out-of-range UIDs),
    or an archive with neither a URL nor a title.  Never raises.
    """
    if not payload:
        return None
    try:
        return _parse(payload)
    except Exception:
        return None


def embedded_images(payload: bytes | None) -> list[bytes] | None:
    """Every PNG/JPEG/ISOBMFF blob over 3000 bytes in the archive, in table order.

    Three outcomes, so an HTTP layer can map each to its own status body
    without the library choosing the words:

    * ``None`` -- ``payload`` is empty, is not a property list, or has no
      ``$objects`` list ("unparseable");
    * ``[]`` -- a well-formed archive that carries no qualifying blob;
    * a non-empty list otherwise; ``max(blobs, key=len)`` is the hero image
      (:func:`embedded_image`), the first of the largest on a tie.

    Only the ``$objects`` table is read, so an archive without ``$top`` still
    yields its blobs (:meth:`KeyedArchive.load` would reject it).  Never raises.
    """
    if not payload:
        return None
    try:
        objects = plistlib.loads(payload)["$objects"]
    except Exception:
        return None
    if not isinstance(objects, list):
        return None
    return _image_blobs(objects)


def embedded_image(payload: bytes | None) -> bytes | None:
    """The largest PNG/JPEG/ISOBMFF blob over 3000 bytes in the archive, or ``None``.

    ``max(embedded_images(payload), key=len)``; ``None`` both when the payload
    is unusable and when it simply has no image -- use :func:`embedded_images`
    to tell the two apart.  Never raises.
    """
    blobs = embedded_images(payload)
    return max(blobs, key=len) if blobs else None


def sniff_image_mime(head: bytes) -> str:
    """Guess an image MIME type from the first bytes of a blob.

    Recognises PNG, JPEG, ISOBMFF (``ftyp`` at offset 4, reported as HEIC),
    GIF and WebP; anything else is ``application/octet-stream``.
    Twelve bytes are enough for every check.
    """
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if head[4:8] == b"ftyp":
        return "image/heic"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"
