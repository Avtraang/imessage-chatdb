"""Pinned behaviours for ``imessage_chatdb.dates`` (DESIGN.md section 8.4)."""

from __future__ import annotations

from datetime import UTC, datetime, timezone

import pytest

from imessage_chatdb.dates import (
    APPLE_EPOCH_OFFSET,
    apple_to_datetime,
    apple_to_unix,
    unix_to_apple,
)

# 2024-01-01T00:00:00Z as Apple-epoch nanoseconds and seconds.
NS_2024 = 725_760_000_000_000_000
S_2024 = 725_760_000
UNIX_2024 = 1_704_067_200.0


def legacy_apple_to_unix(raw: int | float | None) -> float | None:
    """The relay's date expression, copied literally (the legacy oracle)."""
    if not raw:
        return None
    v = float(raw)
    if v > 1e11:
        v = v / 1e9
    return v + 978307200


def test_offset_constant() -> None:
    assert APPLE_EPOCH_OFFSET == 978307200
    assert datetime(2001, 1, 1, tzinfo=UTC).timestamp() == APPLE_EPOCH_OFFSET


@pytest.mark.parametrize("raw", [0, 0.0, None])
def test_sentinel_is_none(raw: int | float | None) -> None:
    assert apple_to_unix(raw) is None
    assert apple_to_datetime(raw) is None


def test_nanoseconds_input() -> None:
    assert apple_to_unix(NS_2024) == UNIX_2024


def test_seconds_input() -> None:
    assert apple_to_unix(S_2024) == UNIX_2024


def test_boundary_1e11_is_seconds_and_above_is_ns() -> None:
    # Exactly 1e11 is not > 1e11, so it is treated as seconds.
    assert apple_to_unix(100_000_000_000) == 100_000_000_000 + APPLE_EPOCH_OFFSET
    assert apple_to_unix(100_000_000_001) == 100_000_000_001 / 1e9 + APPLE_EPOCH_OFFSET


@pytest.mark.parametrize(
    "raw",
    [
        1,
        1.5,
        S_2024,
        NS_2024,
        NS_2024 + 123_456_789,
        725_760_000_123_456_789,
        1,
        99_999_999_999,
        100_000_000_000,
        100_000_000_001,
        788_000_000_000_000_000,
        -5,
        2**62,
    ],
)
def test_bit_identical_with_relay_expression(raw: int | float) -> None:
    ours = apple_to_unix(raw)
    theirs = legacy_apple_to_unix(raw)
    assert ours == theirs
    assert type(ours) is type(theirs)
    assert isinstance(ours, float)


def test_returns_float_even_for_int_input() -> None:
    assert isinstance(apple_to_unix(S_2024), float)


def test_unix_to_apple_round_trip() -> None:
    assert unix_to_apple(UNIX_2024) == NS_2024
    assert apple_to_unix(unix_to_apple(UNIX_2024)) == UNIX_2024
    # Fractional seconds survive to ns precision.
    raw = unix_to_apple(UNIX_2024 + 0.25)
    assert raw == NS_2024 + 250_000_000
    assert apple_to_unix(raw) == UNIX_2024 + 0.25


def test_unix_to_apple_returns_int() -> None:
    assert type(unix_to_apple(UNIX_2024)) is int
    assert unix_to_apple(float(APPLE_EPOCH_OFFSET)) == 0


def test_apple_to_datetime_utc_aware() -> None:
    dt = apple_to_datetime(NS_2024)
    assert dt == datetime(2024, 1, 1, tzinfo=UTC)
    assert dt is not None
    assert dt.tzinfo is UTC
    assert dt.utcoffset() is not None


def test_apple_to_datetime_seconds_input() -> None:
    assert apple_to_datetime(S_2024) == datetime(2024, 1, 1, tzinfo=UTC)


def test_apple_to_datetime_custom_tz_and_naive() -> None:
    from datetime import timedelta

    plus_two = timezone(timedelta(hours=2))
    dt = apple_to_datetime(NS_2024, plus_two)
    assert dt is not None
    assert dt.tzinfo is plus_two
    assert dt.hour == 2
    naive = apple_to_datetime(NS_2024, None)
    assert naive is not None
    assert naive.tzinfo is None
