"""Message service classification (DESIGN.md section 4.3).

``message.service`` takes the values ``iMessage``, ``SMS``, ``RCS`` and
``iMessageLite`` (satellite); ``chat.service_name`` is a stale hint limited to
the first three.  ``normalize_service`` maps a raw string onto :class:`Service`;
``service_family`` is the relay's family rule verbatim, which folds
``iMessageLite`` into ``"iMessage"`` and returns ``None`` for anything else.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["Service", "normalize_service", "service_family"]


class Service(StrEnum):
    """The transport a message or chat used."""

    IMESSAGE = "iMessage"
    IMESSAGE_LITE = "iMessageLite"
    SMS = "SMS"
    RCS = "RCS"
    UNKNOWN = "unknown"


_BY_LOWER: dict[str, Service] = {
    member.value.lower(): member for member in Service if member is not Service.UNKNOWN
}


def normalize_service(s: str | None) -> Service:
    """Map a raw service string onto :class:`Service`; never raises.

    Whitespace is stripped and the comparison is case-insensitive.  ``None``,
    ``""`` and unrecognised values map to :attr:`Service.UNKNOWN`.
    """
    if not s:
        return Service.UNKNOWN
    return _BY_LOWER.get(s.strip().lower(), Service.UNKNOWN)


def service_family(s: str | None) -> str | None:
    """chat.db service string -> ``"iMessage"`` | ``"RCS"`` | ``"SMS"`` | ``None``.

    The relay's expression verbatim: ``iMessageLite`` (satellite) counts as
    iMessage; anything that is not RCS or SMS (case-insensitively) is ``None``.
    """
    s = (s or "").strip()
    if s.startswith("iMessage"):
        return "iMessage"
    if s.upper() in ("RCS", "SMS"):
        return s.upper()
    return None
