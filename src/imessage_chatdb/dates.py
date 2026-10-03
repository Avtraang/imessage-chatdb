"""Apple epoch conversions (DESIGN.md section 4.3).

``chat.db`` stores dates as integers counted from 2001-01-01T00:00:00Z.  On
macOS 10.13+ the unit is nanoseconds; older databases stored seconds.  The
value ``0`` (not ``NULL``) is the sentinel for "never happened" on columns such
as ``date_edited``, ``date_read`` and ``date_retracted``.

``apple_to_unix`` is the relay's date expression verbatim so
that the floats it produces are bit-identical to the relay's HTTP JSON.
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo

__all__ = [
    "APPLE_EPOCH_OFFSET",
    "apple_to_unix",
    "unix_to_apple",
    "apple_to_datetime",
]

#: Seconds between the Unix epoch (1970-01-01) and the Apple epoch (2001-01-01).
APPLE_EPOCH_OFFSET = 978307200


def apple_to_unix(raw: int | float | None) -> float | None:
    """Convert a raw ``chat.db`` date to Unix seconds, or ``None`` for ``0``/``None``.

    Values above ``1e11`` are taken to be nanoseconds (macOS 10.13+) and divided
    by ``1e9``; smaller values are taken to be seconds.  The expression is the
    relay's, kept verbatim for bit-identical floats.
    """
    if not raw:
        return None
    v = float(raw)
    if v > 1e11:
        v = v / 1e9
    return v + APPLE_EPOCH_OFFSET


def unix_to_apple(ts: float) -> int:
    """Convert Unix seconds to an Apple-epoch nanosecond integer."""
    return int(round((ts - APPLE_EPOCH_OFFSET) * 1e9))


def apple_to_datetime(
    raw: int | float | None, tz: tzinfo | None = UTC
) -> datetime | None:
    """Convert a raw ``chat.db`` date to a ``datetime`` (UTC-aware by default).

    Returns ``None`` for the ``0``/``None`` sentinel.  Passing ``tz=None`` yields a
    naive datetime in the local timezone, as ``datetime.fromtimestamp`` does.
    """
    unix = apple_to_unix(raw)
    if unix is None:
        return None
    return datetime.fromtimestamp(unix, tz)
