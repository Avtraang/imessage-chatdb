"""Handle (address) helpers (DESIGN.md section 4.3).

``handle.id`` and ``chat.chat_identifier`` are raw strings: phone numbers in
assorted formattings or e-mail addresses.  ``address_key`` reduces them to a
comparison key the way the relay's own address normaliser does for
non-contact input:
e-mails are lower-cased, phone numbers are reduced to their digits and, when
there are at least ten of them, to the last ten (national number, US-biased).
It is the default ``key`` of ``find_chat``; callers with a contacts table pass
their own.
"""

from __future__ import annotations

__all__ = ["address_key", "is_email", "is_group_style"]

#: ``chat.style`` value for a group chat; ``45`` is one-to-one.
GROUP_STYLE = 43


def address_key(addr: str | None) -> str | None:
    """Comparison key for a handle: ``None``/``""`` -> ``None``.

    An address containing ``@`` is ``strip().lower()``-ed.  Anything else is
    reduced to its digits, and to the last ten digits when there are ten or more.
    """
    if not addr:
        return None
    if "@" in addr:
        return addr.strip().lower()
    digits = "".join(ch for ch in addr if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def is_email(addr: str) -> bool:
    """True when ``addr`` is an e-mail style handle (contains ``@``)."""
    return "@" in addr


def is_group_style(style: int | None) -> bool:
    """True when ``chat.style`` denotes a group chat (``43``); ``45`` is one-to-one."""
    return style == GROUP_STYLE
