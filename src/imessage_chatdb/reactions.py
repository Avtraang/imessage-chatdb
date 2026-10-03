"""Tapback (reaction) classification (DESIGN.md section 4.3).

``message.associated_message_type`` encodes tapbacks: ``2000``-``2006`` add a
reaction, ``3000``-``3006`` remove the matching one, ``1000`` is a sticker placed
on a message, and ``0`` is an ordinary message.  ``2006``/``3006`` are the
macOS 26+ custom-emoji tapbacks whose emoji lives in
``message.associated_message_emoji``.

``message.associated_message_guid`` names the target: ``p:<part>/<GUID>``
(dominant), ``bp:<GUID>`` (balloon part) or a bare ``<GUID>``.

``Reaction.kind`` is a lowercase token, never a presentation string; verbs such
as "Loved" belong to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["Reaction", "classify_reaction", "parse_associated_guid"]

#: Offset-indexed kinds shared by the 2000 (add) and 3000 (remove) ranges.
_KINDS: tuple[str, ...] = ("love", "like", "dislike", "laugh", "emphasize", "question", "emoji")

STICKER_TYPE = 1000
ADD_BASE = 2000
REMOVE_BASE = 3000


@dataclass(frozen=True, slots=True)
class Reaction:
    """A classified tapback.

    ``kind`` is one of ``love``, ``like``, ``dislike``, ``laugh``, ``emphasize``,
    ``question``, ``emoji``, ``sticker`` or ``unknown``.  ``raw_type`` is the
    untouched ``associated_message_type``.
    """

    action: Literal["add", "remove"]
    kind: str
    emoji: str | None
    raw_type: int
    target_guid: str | None
    target_part: int | None
    is_balloon: bool


def parse_associated_guid(s: str | None) -> tuple[str | None, int | None, bool]:
    """Split ``associated_message_guid`` into ``(target_guid, part, is_balloon)``.

    ``"p:3/G"`` -> ``("G", 3, False)``; ``"bp:G"`` -> ``("G", None, True)``;
    ``"G"`` -> ``("G", None, False)``; ``None``/``""`` -> ``(None, None, False)``.
    A ``p:`` prefix whose part is not an integer yields ``part=None``.
    """
    if not s:
        return (None, None, False)
    if s.startswith("bp:"):
        guid = s[3:]
        return (guid or None, None, True)
    if s.startswith("p:"):
        rest = s[2:]
        part_text, sep, guid = rest.partition("/")
        if not sep:
            # "p:G" with no part separator: treat the remainder as the guid.
            return (rest or None, None, False)
        part = int(part_text) if part_text.isdigit() else None
        return (guid or None, part, False)
    return (s, None, False)


def classify_reaction(
    assoc_type: int | None, assoc_guid: str | None, emoji: str | None = None
) -> Reaction | None:
    """Classify ``associated_message_type``; ``0``/``None`` -> ``None`` (not a tapback).

    ``2000``..``2006`` -> ``add`` of love/like/dislike/laugh/emphasize/question/emoji;
    ``3000``..``3006`` -> the matching ``remove``; ``1000`` -> ``add`` of ``sticker``;
    any other non-zero value -> kind ``unknown`` with ``add`` below ``3000`` and
    ``remove`` otherwise.  ``emoji`` is carried through untouched.
    """
    if not assoc_type:
        return None
    target_guid, target_part, is_balloon = parse_associated_guid(assoc_guid)
    action: Literal["add", "remove"]
    if ADD_BASE <= assoc_type < ADD_BASE + len(_KINDS):
        action, kind = "add", _KINDS[assoc_type - ADD_BASE]
    elif REMOVE_BASE <= assoc_type < REMOVE_BASE + len(_KINDS):
        action, kind = "remove", _KINDS[assoc_type - REMOVE_BASE]
    elif assoc_type == STICKER_TYPE:
        action, kind = "add", "sticker"
    else:
        action, kind = ("add" if assoc_type < REMOVE_BASE else "remove"), "unknown"
    return Reaction(
        action=action,
        kind=kind,
        emoji=emoji,
        raw_type=assoc_type,
        target_guid=target_guid,
        target_part=target_part,
        is_balloon=is_balloon,
    )
