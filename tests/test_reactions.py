"""Pinned behaviours for ``imessage_chatdb.reactions`` (DESIGN.md section 8.4)."""

from __future__ import annotations

import dataclasses

import pytest

from imessage_chatdb.reactions import Reaction, classify_reaction, parse_associated_guid

GUID = "A1B2C3D4-0000-4000-8000-00000000ABCD"
KINDS = ["love", "like", "dislike", "laugh", "emphasize", "question", "emoji"]


# ---------- parse_associated_guid ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"p:3/{GUID}", (GUID, 3, False)),
        (f"p:0/{GUID}", (GUID, 0, False)),
        (f"p:12/{GUID}", (GUID, 12, False)),
        (f"bp:{GUID}", (GUID, None, True)),
        (GUID, (GUID, None, False)),
        (None, (None, None, False)),
        ("", (None, None, False)),
    ],
)
def test_parse_associated_guid_forms(
    raw: str | None, expected: tuple[str | None, int | None, bool]
) -> None:
    assert parse_associated_guid(raw) == expected


def test_parse_associated_guid_non_numeric_part() -> None:
    assert parse_associated_guid(f"p:x/{GUID}") == (GUID, None, False)


def test_parse_associated_guid_p_without_slash() -> None:
    assert parse_associated_guid(f"p:{GUID}") == (GUID, None, False)


def test_parse_associated_guid_guid_containing_slash_is_kept_whole() -> None:
    # Only the first "/" after "p:<part>" splits; anything after belongs to the guid.
    assert parse_associated_guid("p:1/a/b") == ("a/b", 1, False)


def test_parse_associated_guid_empty_prefixed() -> None:
    assert parse_associated_guid("bp:") == (None, None, True)
    assert parse_associated_guid("p:") == (None, None, False)


# ---------- classify_reaction ----------


@pytest.mark.parametrize("raw", [0, None])
def test_plain_rows_are_not_reactions(raw: int | None) -> None:
    assert classify_reaction(raw, None) is None
    assert classify_reaction(raw, f"p:0/{GUID}") is None


@pytest.mark.parametrize(("offset", "kind"), list(enumerate(KINDS)))
def test_add_range_2000_2006(offset: int, kind: str) -> None:
    r = classify_reaction(2000 + offset, f"p:0/{GUID}")
    assert r == Reaction(
        action="add",
        kind=kind,
        emoji=None,
        raw_type=2000 + offset,
        target_guid=GUID,
        target_part=0,
        is_balloon=False,
    )


@pytest.mark.parametrize(("offset", "kind"), list(enumerate(KINDS)))
def test_remove_range_3000_3006(offset: int, kind: str) -> None:
    r = classify_reaction(3000 + offset, f"p:0/{GUID}")
    assert r is not None
    assert r.action == "remove"
    assert r.kind == kind
    assert r.raw_type == 3000 + offset


def test_sticker_1000() -> None:
    r = classify_reaction(1000, f"bp:{GUID}")
    assert r is not None
    assert r.action == "add"
    assert r.kind == "sticker"
    assert r.raw_type == 1000
    assert r.target_guid == GUID
    assert r.target_part is None
    assert r.is_balloon is True


def test_emoji_tapback_carries_emoji() -> None:
    r = classify_reaction(2006, f"p:0/{GUID}", "\U0001f525")
    assert r is not None
    assert r.kind == "emoji"
    assert r.action == "add"
    assert r.emoji == "\U0001f525"
    rr = classify_reaction(3006, f"p:0/{GUID}", "\U0001f525")
    assert rr is not None
    assert rr.kind == "emoji"
    assert rr.action == "remove"
    assert rr.emoji == "\U0001f525"


def test_emoji_passed_through_for_other_kinds() -> None:
    # The library records the fact; it does not second-guess the column.
    r = classify_reaction(2000, None, "x")
    assert r is not None
    assert r.emoji == "x"


@pytest.mark.parametrize(
    ("raw", "action"),
    [(1, "add"), (999, "add"), (1001, "add"), (2007, "add"), (2999, "add"),
     (3007, "remove"), (3999, "remove"), (4000, "remove"), (10**6, "remove"), (-1, "add")],
)
def test_unknown_non_zero_types(raw: int, action: str) -> None:
    r = classify_reaction(raw, None)
    assert r is not None
    assert r.kind == "unknown"
    assert r.action == action
    assert r.raw_type == raw
    assert (r.target_guid, r.target_part, r.is_balloon) == (None, None, False)


@pytest.mark.parametrize(
    ("guid", "expected"),
    [
        (f"p:3/{GUID}", (GUID, 3, False)),
        (f"bp:{GUID}", (GUID, None, True)),
        (GUID, (GUID, None, False)),
        (None, (None, None, False)),
    ],
)
def test_classify_uses_all_guid_forms(
    guid: str | None, expected: tuple[str | None, int | None, bool]
) -> None:
    r = classify_reaction(2001, guid)
    assert r is not None
    assert (r.target_guid, r.target_part, r.is_balloon) == expected


def test_reaction_is_frozen_slotted_dataclass() -> None:
    r = classify_reaction(2000, f"p:0/{GUID}")
    assert r is not None
    assert dataclasses.is_dataclass(r)
    assert not hasattr(r, "__dict__")  # slots=True
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.kind = "like"  # type: ignore[misc]
    assert [f.name for f in dataclasses.fields(Reaction)] == [
        "action", "kind", "emoji", "raw_type", "target_guid", "target_part", "is_balloon",
    ]


def test_kind_is_lowercase_token_not_verb() -> None:
    for t in range(2000, 2007):
        r = classify_reaction(t, None)
        assert r is not None
        assert r.kind == r.kind.lower()
        assert r.kind in {*KINDS, "sticker", "unknown"}
