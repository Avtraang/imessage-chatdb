"""Pinned behaviours for ``imessage_chatdb.services`` and ``handles`` (DESIGN.md 8.4)."""

from __future__ import annotations

from enum import StrEnum

import pytest

from imessage_chatdb.handles import address_key, is_email, is_group_style
from imessage_chatdb.services import Service, normalize_service, service_family


def legacy_service_family(s: str | None) -> str | None:
    """The relay's service-family expression, copied literally (the legacy oracle)."""
    s = (s or "").strip()
    if s.startswith("iMessage"):
        return "iMessage"
    if s.upper() in ("RCS", "SMS"):
        return s.upper()
    return None


SERVICE_INPUTS: list[str | None] = [
    None,
    "",
    "   ",
    "iMessage",
    "iMessageLite",
    " iMessage ",
    "imessage",
    "IMESSAGE",
    "iMessageX",
    "SMS",
    "sms",
    " Sms ",
    "RCS",
    "rcs",
    "Rcs",
    "MMS",
    "unknown",
    "Lite",
    "SMS ",
]


# ---------- Service / normalize_service ----------


def test_service_is_strenum_with_expected_values() -> None:
    assert issubclass(Service, StrEnum)
    assert [m.value for m in Service] == ["iMessage", "iMessageLite", "SMS", "RCS", "unknown"]
    assert str(Service.SMS) == "SMS"
    assert isinstance(Service.IMESSAGE_LITE, str)
    assert f"{Service.RCS}" == "RCS"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("iMessage", Service.IMESSAGE),
        ("iMessageLite", Service.IMESSAGE_LITE),
        ("SMS", Service.SMS),
        ("RCS", Service.RCS),
    ],
)
def test_normalize_service_four_raw_values(raw: str, expected: Service) -> None:
    assert normalize_service(raw) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("imessage", Service.IMESSAGE),
        ("IMESSAGELITE", Service.IMESSAGE_LITE),
        (" sms ", Service.SMS),
        ("\trcs\n", Service.RCS),
    ],
)
def test_normalize_service_strips_and_ignores_case(raw: str, expected: Service) -> None:
    assert normalize_service(raw) is expected


@pytest.mark.parametrize("raw", [None, "", "   ", "MMS", "iMessageX", "unknown", "Lite"])
def test_normalize_service_unknown(raw: str | None) -> None:
    assert normalize_service(raw) is Service.UNKNOWN


def test_normalize_service_never_raises_on_odd_strings() -> None:
    for raw in ("\x00", "a" * 10_000, "émessage", "iMessage\x00"):
        assert isinstance(normalize_service(raw), Service)


# ---------- service_family ----------


@pytest.mark.parametrize("raw", SERVICE_INPUTS)
def test_service_family_equals_legacy_expression(raw: str | None) -> None:
    assert service_family(raw) == legacy_service_family(raw)


def test_service_family_pins() -> None:
    assert service_family("iMessage") == "iMessage"
    assert service_family("iMessageLite") == "iMessage"  # satellite folds into iMessage
    assert service_family("SMS") == "SMS"
    assert service_family("sms") == "SMS"
    assert service_family("RCS") == "RCS"
    assert service_family("rcs") == "RCS"
    assert service_family("imessage") is None  # prefix check is case-sensitive, as the relay
    assert service_family("MMS") is None
    assert service_family("") is None
    assert service_family(None) is None


# ---------- handles ----------


@pytest.mark.parametrize("raw", [None, ""])
def test_address_key_empty(raw: str | None) -> None:
    assert address_key(raw) is None


def test_address_key_email_lowercased_and_stripped() -> None:
    assert address_key("  Test@Example.INVALID ") == "test@example.invalid"
    assert address_key("test@example.invalid") == "test@example.invalid"


def test_address_key_phone_last_ten_digits() -> None:
    assert address_key("+15550001234") == "5550001234"
    assert address_key("(555) 000-1234") == "5550001234"
    assert address_key("1 555 000 1234") == "5550001234"
    assert address_key("+1 (555) 000-1234") == "5550001234"


def test_address_key_short_number_keeps_all_digits() -> None:
    assert address_key("12345") == "12345"
    assert address_key("555-0123") == "5550123"


def test_address_key_exactly_ten_digits() -> None:
    assert address_key("5550001234") == "5550001234"


def test_address_key_is_north_american_and_collides_on_international_numbers() -> None:
    """The documented caveat: last-10-digits is NANP; non-NANP numbers collide/mismatch."""
    import imessage_chatdb.handles as handles

    # two different international numbers sharing their last ten digits collide
    assert address_key("+44 20 7946 0958") == address_key("+33 20 7946 0958") == "2079460958"
    # the same number with and without its country code keys differently once
    # the national part alone is shorter than ten digits
    assert address_key("+49 30 123456") != address_key("030 123456")
    # the docstrings say so and show the escape hatch
    assert handles.__doc__ is not None
    assert "North American" in handles.__doc__ and "key=" in handles.__doc__
    assert address_key.__doc__ is not None and "international" in address_key.__doc__
    # the one-line override from the docs: compare full digit strings
    full = lambda a: address_key(a) if "@" in a else "".join(ch for ch in a if ch.isdigit())  # noqa: E731
    assert full("+44 20 7946 0958") != full("+33 20 7946 0958")


def test_address_key_no_digits_is_empty_string() -> None:
    # A phone-style string with no digits reduces to '' (the relay's normalize_phone result).
    assert address_key("abc") == ""


def test_address_key_collapses_formattings_to_one_key() -> None:
    keys = {address_key(a) for a in ("+15550001234", "5550001234", "555.000.1234", "15550001234")}
    assert keys == {"5550001234"}


def test_is_email() -> None:
    assert is_email("test@example.invalid") is True
    assert is_email("+15550001234") is False
    assert is_email("") is False


@pytest.mark.parametrize(
    ("style", "expected"),
    [(43, True), (45, False), (0, False), (None, False), (-1, False)],
)
def test_is_group_style(style: int | None, expected: bool) -> None:
    assert is_group_style(style) is expected
