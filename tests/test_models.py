"""Tests for ``imessage_chatdb.models`` (DESIGN.md sections 5, 5.1, T5).

Pure in-memory tests: no database is opened.  Only synthetic handles appear
(``+1555...`` numbers and ``*@example.invalid`` addresses).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from imessage_chatdb import models
from imessage_chatdb.dates import APPLE_EPOCH_OFFSET, apple_to_unix
from imessage_chatdb.link_preview import LinkPreview
from imessage_chatdb.models import (
    GROUP_STYLE,
    PLUGIN_PAYLOAD_SUFFIX,
    Attachment,
    Chat,
    ChatMatch,
    ChatSummary,
    LiteMessage,
    Message,
    ReplyTarget,
    SearchHit,
)
from imessage_chatdb.reactions import Reaction
from imessage_chatdb.services import Service, normalize_service
from imessage_chatdb.typedstream import clean_text

PHONE = "+15550001234"
OTHER_PHONE = "+15550009876"
EMAIL = "test@example.invalid"

# Apple ns for 2024-01-01T00:00:00Z (same constant as the fixture builders).
DATE_NS = 725_760_000_000_000_000

ALL_MODELS: tuple[type, ...] = (
    Message,
    Attachment,
    ReplyTarget,
    LiteMessage,
    Chat,
    ChatSummary,
    ChatMatch,
    SearchHit,
)

RELAY_MESSAGE_KEYS = [
    "rowid",
    "guid",
    "text",
    "date",
    "date_read",
    "date_edited",
    "is_from_me",
    "sender",
    "sender_handle",
    "chat_guid",
    "chat_name",
    "is_group",
    "has_attachments",
    "assoc_guid",
    "assoc_type",
    "attachments",
    "link",
    "reply_to_guid",
    "reply_to",
    "service",
]


# ---------- helpers ----------


def make_attachment(**over: Any) -> Attachment:
    base: dict[str, Any] = {
        "message_rowid": 1,
        "rowid": 10,
        "guid": "SYN-ATT-0001",
        "mime_type": "image/png",
        "transfer_name": "IMG_0001.png",
        "filename": "~/synthetic/Attachments/ab/11/SYN/IMG_0001.png",
        "uti": "public.png",
        "total_bytes": 1234,
        "is_sticker": False,
        "hide_attachment": False,
        "message_date": DATE_NS,
    }
    base.update(over)
    return Attachment(**base)


def make_message(**over: Any) -> Message:
    base: dict[str, Any] = {
        "rowid": 1,
        "guid": "SYN-MSG-000001",
        "text": "hello there",
        "text_column": "hello there",
        "date": DATE_NS,
        "date_read": 0,
        "date_delivered": 0,
        "date_edited": 0,
        "date_retracted": 0,
        "is_from_me": False,
        "handle_rowid": 1,
        "sender_handle": PHONE,
        "chat_rowid": 1,
        "chat_guid": f"any;-;{PHONE}",
        "chat_identifier": PHONE,
        "chat_display_name": None,
        "chat_style": 45,
        "service_raw": "iMessage",
        "has_attachments": False,
        "associated_guid": None,
        "associated_type": 0,
        "associated_emoji": None,
        "reply_to_guid": None,
        "reply_to_part": None,
        "balloon_bundle_id": None,
        "link": None,
        "item_type": 0,
        "group_action_type": 0,
        "group_title": None,
        "filter_action": 0,
        "is_spam": False,
        "expressive_send_style_id": None,
    }
    base.update(over)
    return Message(**base)


def make_chat(**over: Any) -> Chat:
    base: dict[str, Any] = {
        "rowid": 1,
        "guid": f"any;-;{PHONE}",
        "style": 45,
        "chat_identifier": PHONE,
        "display_name": None,
        "service_name": "iMessage",
        "is_archived": False,
        "is_filtered": False,
        "group_id": None,
    }
    base.update(over)
    return Chat(**base)


def make_summary(**over: Any) -> ChatSummary:
    base: dict[str, Any] = {
        "rowid": 1,
        "guid": f"any;-;{PHONE}",
        "style": 45,
        "chat_identifier": PHONE,
        "display_name": None,
        "service_name": "iMessage",
        "is_archived": False,
        "is_filtered": False,
        "group_id": None,
        "last_date": DATE_NS,
        "last_rowid": 7,
    }
    base.update(over)
    return ChatSummary(**base)


def make_hit(**over: Any) -> SearchHit:
    base: dict[str, Any] = {
        "rowid": 3,
        "chat_rowid": 1,
        "chat_guid": f"any;-;{PHONE}",
        "chat_identifier": PHONE,
        "chat_display_name": None,
        "is_group": False,
        "date": DATE_NS,
        "is_from_me": True,
        "sender_handle": None,
        "text": "meet at noon",
        "match_index": 8,
    }
    base.update(over)
    return SearchHit(**base)


def field_names(cls: type) -> list[str]:
    return [f.name for f in dataclasses.fields(cls)]


def legacy_apple_to_unix(raw: int | float | None) -> float | None:
    """The relay's date expression, literally (the legacy oracle)."""
    if not raw:
        return None
    v = float(raw)
    if v > 1e11:
        v = v / 1e9
    return v + APPLE_EPOCH_OFFSET


# ---------- dataclass shape ----------


@pytest.mark.parametrize("cls", ALL_MODELS, ids=lambda c: c.__name__)
def test_every_model_is_a_frozen_slotted_dataclass(cls: type) -> None:
    assert dataclasses.is_dataclass(cls)
    params = cls.__dataclass_params__  # type: ignore[attr-defined]
    assert params.frozen is True
    assert params.slots is True
    assert "__slots__" in cls.__dict__


def test_instances_have_no_dict_and_reject_assignment() -> None:
    instances: list[Any] = [
        make_message(),
        make_attachment(),
        ReplyTarget("G", "t", False, PHONE),
        LiteMessage(1, "t", False, 0, None, False, PHONE, ()),
        make_chat(),
        make_summary(),
        ChatMatch(1, "any;-;x", PHONE, None, False, 5),
        make_hit(),
    ]
    for obj in instances:
        assert not hasattr(obj, "__dict__"), type(obj).__name__
        first = dataclasses.fields(obj)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, first, getattr(obj, first))
        # Unknown attributes are rejected too (no __dict__).  CPython 3.12 raises
        # TypeError here from the slots-dataclass __setattr__ (gh-90562); 3.13+
        # raise FrozenInstanceError/AttributeError.  Either way nothing is set.
        unknown = "not_a_field"
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError, TypeError)):
            setattr(obj, unknown, 1)
        assert not hasattr(obj, unknown)


def test_message_fields_match_design_section_5() -> None:
    assert field_names(Message) == [
        "rowid",
        "guid",
        "text",
        "text_column",
        "date",
        "date_read",
        "date_delivered",
        "date_edited",
        "date_retracted",
        "is_from_me",
        "handle_rowid",
        "sender_handle",
        "chat_rowid",
        "chat_guid",
        "chat_identifier",
        "chat_display_name",
        "chat_style",
        "service_raw",
        "has_attachments",
        "attachments",
        "associated_guid",
        "associated_type",
        "associated_emoji",
        "reply_to_guid",
        "reply_to_part",
        "reply_to",
        "balloon_bundle_id",
        "link",
        "item_type",
        "group_action_type",
        "group_title",
        "filter_action",
        "is_spam",
        "expressive_send_style_id",
        "enriched",
    ]


def test_other_fields_match_design_section_5() -> None:
    assert field_names(Attachment) == [
        "message_rowid",
        "rowid",
        "guid",
        "mime_type",
        "transfer_name",
        "filename",
        "uti",
        "total_bytes",
        "is_sticker",
        "hide_attachment",
        "message_date",
    ]
    assert field_names(ReplyTarget) == ["guid", "text", "is_from_me", "sender_handle"]
    assert field_names(LiteMessage) == [
        "rowid",
        "text",
        "is_from_me",
        "associated_type",
        "associated_emoji",
        "has_attachments",
        "sender_handle",
        "attachments",
    ]
    assert field_names(Chat) == [
        "rowid",
        "guid",
        "style",
        "chat_identifier",
        "display_name",
        "service_name",
        "is_archived",
        "is_filtered",
        "group_id",
    ]
    assert field_names(ChatSummary) == field_names(Chat) + ["last_date", "last_rowid"]
    assert issubclass(ChatSummary, Chat)
    assert field_names(ChatMatch) == [
        "chat_rowid",
        "chat_guid",
        "chat_identifier",
        "display_name",
        "is_group",
        "last_rowid",
    ]
    assert field_names(SearchHit) == [
        "rowid",
        "chat_rowid",
        "chat_guid",
        "chat_identifier",
        "chat_display_name",
        "is_group",
        "date",
        "is_from_me",
        "sender_handle",
        "text",
        "match_index",
    ]


def test_message_is_keyword_only_with_enrichment_defaults() -> None:
    m = make_message()
    assert m.attachments == ()
    assert m.reply_to is None
    assert m.enriched is False
    with pytest.raises(TypeError):
        Message(1, "G")  # type: ignore[call-arg]
    att = make_attachment()
    target = ReplyTarget("SYN-MSG-000000", "earlier", True, None)
    enriched = dataclasses.replace(m, attachments=(att,), reply_to=target, enriched=True)
    assert enriched.attachments == (att,)
    assert enriched.reply_to is target
    assert enriched.enriched is True
    assert m.attachments == ()  # original untouched


def test_models_are_hashable_and_compare_by_value() -> None:
    link = LinkPreview(
        url="https://example.invalid/a",
        title="A",
        summary=None,
        site=None,
        image_url=None,
        has_embedded_image=False,
    )
    a = make_message(link=link, attachments=(make_attachment(),))
    b = make_message(link=link, attachments=(make_attachment(),))
    assert a == b
    assert hash(a) == hash(b)
    assert len({a, b}) == 1
    assert make_chat() == make_chat()
    assert make_summary() != make_summary(last_rowid=8)


def test_constants() -> None:
    assert GROUP_STYLE == 43
    assert PLUGIN_PAYLOAD_SUFFIX == ".pluginPayloadAttachment"


# ---------- Message properties ----------


@pytest.mark.parametrize(
    ("style", "expected"),
    [(43, True), (45, False), (None, False), (0, False)],
)
def test_message_is_group(style: int | None, expected: bool) -> None:
    m = make_message(chat_style=style)
    assert m.is_group is expected


@pytest.mark.parametrize("raw", ["iMessage", "SMS", "RCS", "iMessageLite", " sms ", "bogus", None])
def test_message_service_uses_normalize_service(raw: str | None) -> None:
    m = make_message(service_raw=raw)
    assert isinstance(m.service, Service)
    assert m.service is normalize_service(raw)
    assert m.service_raw == raw  # raw value untouched


def test_message_service_concrete_values() -> None:
    assert make_message(service_raw="iMessage").service is Service.IMESSAGE
    assert make_message(service_raw="SMS").service is Service.SMS
    assert make_message(service_raw="RCS").service is Service.RCS
    assert make_message(service_raw="iMessageLite").service is Service.IMESSAGE_LITE
    assert make_message(service_raw=None).service is Service.UNKNOWN


@pytest.mark.parametrize("assoc_type", [0, None])
def test_plain_message_has_no_reaction(assoc_type: int | None) -> None:
    m = make_message(associated_type=assoc_type, associated_guid=None)
    assert m.reaction is None
    assert m.is_tapback is False


def test_tapback_message_reaction() -> None:
    m = make_message(
        text=None,
        text_column=None,
        associated_type=2000,
        associated_guid="p:0/SYN-MSG-000000",
    )
    r = m.reaction
    assert isinstance(r, Reaction)
    assert r.action == "add"
    assert r.kind == "love"
    assert r.raw_type == 2000
    assert r.target_guid == "SYN-MSG-000000"
    assert r.target_part == 0
    assert r.is_balloon is False
    assert m.is_tapback is True


def test_tapback_remove_and_emoji_and_sticker() -> None:
    rem = make_message(associated_type=3001, associated_guid="bp:SYN-MSG-000000").reaction
    assert rem is not None
    assert (rem.action, rem.kind, rem.is_balloon, rem.target_part) == ("remove", "like", True, None)
    emo = make_message(
        associated_type=2006, associated_guid="SYN-MSG-000000", associated_emoji="\U0001f525"
    ).reaction
    assert emo is not None
    assert (emo.action, emo.kind, emo.emoji) == ("add", "emoji", "\U0001f525")
    assert emo.target_guid == "SYN-MSG-000000"
    sticker = make_message(associated_type=1000, associated_guid="p:0/SYN-MSG-000000").reaction
    assert sticker is not None
    assert sticker.kind == "sticker"
    assert make_message(associated_type=1000, associated_guid="p:0/X").is_tapback is True


def test_date_properties_match_relay_expression() -> None:
    m = make_message(
        date=DATE_NS,
        date_read=DATE_NS + 5_000_000_000,
        date_delivered=DATE_NS + 1_000_000_000,
        date_edited=DATE_NS + 60_000_000_000,
        date_retracted=DATE_NS + 61_000_000_000,
    )
    for prop, raw in [
        ("date_unix", m.date),
        ("date_read_unix", m.date_read),
        ("date_delivered_unix", m.date_delivered),
        ("date_edited_unix", m.date_edited),
        ("date_retracted_unix", m.date_retracted),
    ]:
        got = getattr(m, prop)
        assert got == apple_to_unix(raw)
        assert got == legacy_apple_to_unix(raw)
        assert isinstance(got, float)
    assert m.date_unix == 1704067200.0


def test_date_zero_and_none_are_never() -> None:
    m = make_message(date_read=0, date_edited=0, date_retracted=None, date_delivered=None)
    assert m.date_read_unix is None
    assert m.date_edited_unix is None
    assert m.date_retracted_unix is None
    assert m.date_delivered_unix is None
    # raw values stay raw on the model
    assert m.date_read == 0
    assert m.date_retracted is None


def test_date_seconds_heuristic_passthrough() -> None:
    # pre-10.13 databases store seconds; apple_to_unix applies the > 1e11 heuristic
    secs = 725_760_000
    m = make_message(date=secs)
    assert m.date_unix == legacy_apple_to_unix(secs) == secs + APPLE_EPOCH_OFFSET


def test_datetime_is_utc_aware() -> None:
    m = make_message(date=DATE_NS)
    d = m.datetime
    assert isinstance(d, dt.datetime)
    assert d.tzinfo is not None
    assert d.utcoffset() == dt.timedelta(0)
    assert d == dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
    assert make_message(date=0).datetime is None


def test_clean_text_property() -> None:
    raw = "￼  hello\n\n  world \t!"
    m = make_message(text=raw, text_column=raw)
    assert m.clean_text == clean_text(raw) == "hello world !"
    assert make_message(text=None, text_column=None).clean_text == ""
    assert make_message(text="￼", text_column=None).clean_text == ""
    # the raw field is untouched
    assert m.text == raw


def test_chat_name_property() -> None:
    assert make_message(chat_display_name=None, chat_identifier=PHONE).chat_name == PHONE
    assert make_message(chat_display_name="Team", chat_identifier="chat1").chat_name == "Team"
    assert make_message(chat_display_name="", chat_identifier="chat1").chat_name == "chat1"
    assert make_message(chat_display_name=None, chat_identifier=None).chat_name is None


def test_bool_fields_are_bool() -> None:
    m = make_message(is_from_me=True, has_attachments=False)
    assert m.is_from_me is True
    assert m.has_attachments is False
    assert m.is_group is False
    assert m.is_tapback is False
    d = m.to_dict()
    assert d["is_from_me"] is True
    assert d["has_attachments"] is False
    assert d["is_group"] is False


# ---------- Message.to_dict ----------


def test_to_dict_key_order_is_relay_order() -> None:
    assert list(make_message().to_dict()) == RELAY_MESSAGE_KEYS
    rich = make_message(
        has_attachments=True,
        attachments=(make_attachment(),),
        link=LinkPreview("https://example.invalid", "T", None, None, None, False),
        reply_to_guid="SYN-MSG-000000",
        reply_to=ReplyTarget("SYN-MSG-000000", "earlier", False, PHONE),
        balloon_bundle_id="com.apple.messages.URLBalloonProvider",
    )
    assert list(rich.to_dict()) == RELAY_MESSAGE_KEYS


def test_to_dict_plain_values() -> None:
    m = make_message(
        rowid=42,
        guid="SYN-MSG-000042",
        text="hi",
        date=DATE_NS,
        date_read=DATE_NS + 2_000_000_000,
        date_edited=0,
        is_from_me=False,
        sender_handle=PHONE,
        chat_guid=f"any;-;{PHONE}",
        chat_identifier=PHONE,
        chat_display_name=None,
        chat_style=45,
        service_raw="SMS",
        associated_guid=None,
        associated_type=0,
    )
    d = m.to_dict()
    assert d == {
        "rowid": 42,
        "guid": "SYN-MSG-000042",
        "text": "hi",
        "date": 1704067200.0,
        "date_read": 1704067202.0,
        "date_edited": None,
        "is_from_me": False,
        "sender": PHONE,
        "sender_handle": PHONE,
        "chat_guid": f"any;-;{PHONE}",
        "chat_name": PHONE,
        "is_group": False,
        "has_attachments": False,
        "assoc_guid": None,
        "assoc_type": 0,
        "attachments": [],
        "link": None,
        "reply_to_guid": None,
        "reply_to": None,
        "service": "SMS",
    }
    # dates are the relay's floats
    assert d["date"] == legacy_apple_to_unix(DATE_NS)
    assert d["date_read"] == legacy_apple_to_unix(DATE_NS + 2_000_000_000)


def test_to_dict_sender_is_raw_handle_not_resolved() -> None:
    d = make_message(sender_handle=EMAIL).to_dict()
    assert d["sender"] == EMAIL
    assert d["sender_handle"] == EMAIL
    d2 = make_message(sender_handle=None, is_from_me=True).to_dict()
    assert d2["sender"] is None
    assert d2["sender_handle"] is None


def test_to_dict_group_chat_name_and_text_variants() -> None:
    g = make_message(
        chat_style=43,
        chat_guid="any;+;chat123",
        chat_identifier="chat123",
        chat_display_name="Weekend",
    ).to_dict()
    assert g["is_group"] is True
    assert g["chat_name"] == "Weekend"
    g2 = make_message(chat_style=43, chat_identifier="chat123", chat_display_name=None).to_dict()
    assert g2["chat_name"] == "chat123"
    # text is passed through raw: '' stays '', None stays None (effective_text decided upstream)
    assert make_message(text="", text_column="").to_dict()["text"] == ""
    assert make_message(text=None, text_column=None).to_dict()["text"] is None
    assert make_message(text="a￼ b", text_column=None).to_dict()["text"] == "a￼ b"


def test_to_dict_attachments_shape_without_url() -> None:
    a1 = make_attachment(guid="SYN-ATT-0001", mime_type="image/heic", transfer_name="IMG_1.heic")
    a2 = make_attachment(
        guid="SYN-ATT-0002",
        mime_type=None,
        transfer_name=None,
        filename=None,
        rowid=11,
    )
    d = make_message(has_attachments=True, attachments=(a1, a2)).to_dict()
    assert d["attachments"] == [
        {
            "guid": "SYN-ATT-0001",
            "mime_type": "image/heic",
            "name": "IMG_1.heic",
            "filename": a1.filename,
        },
        {"guid": "SYN-ATT-0002", "mime_type": None, "name": None, "filename": None},
    ]
    for item in d["attachments"]:
        assert list(item) == ["guid", "mime_type", "name", "filename"]
        assert "url" not in item
    assert isinstance(d["attachments"], list)


def test_to_dict_link_shape() -> None:
    lp = LinkPreview(
        url="https://example.invalid/page",
        title="Example",
        summary="A summary",
        site="example.invalid",
        image_url="https://example.invalid/og.png",
        has_embedded_image=False,
    )
    d = make_message(link=lp, balloon_bundle_id="com.apple.messages.URLBalloonProvider").to_dict()
    assert d["link"] == {
        "url": "https://example.invalid/page",
        "title": "Example",
        "summary": "A summary",
        "site": "example.invalid",
        "image": "https://example.invalid/og.png",
    }
    assert list(d["link"]) == ["url", "title", "summary", "site", "image"]
    # embedded blob only -> image is None (fetch with db.link_image); no relay path string
    blob_only = dataclasses.replace(lp, image_url=None, has_embedded_image=True)
    d2 = make_message(link=blob_only).to_dict()
    assert d2["link"]["image"] is None
    assert "/link_image/" not in json.dumps(d2)
    assert make_message(link=None).to_dict()["link"] is None


def test_to_dict_reply_is_facts_only_no_strings_no_truncation() -> None:
    long_text = "x" * 200
    d = make_message(
        reply_to_guid="SYN-MSG-000000",
        reply_to=ReplyTarget("SYN-MSG-000000", long_text, False, OTHER_PHONE),
    ).to_dict()
    assert d["reply_to_guid"] == "SYN-MSG-000000"
    assert d["reply_to"] == {
        "guid": "SYN-MSG-000000",
        "text": long_text,  # untruncated
        "is_from_me": False,
        "sender_handle": OTHER_PHONE,
    }
    assert list(d["reply_to"]) == ["guid", "text", "is_from_me", "sender_handle"]
    # attachment-only target stays ''; outgoing is a bool; NULL handle stays None
    d2 = make_message(
        reply_to_guid="G", reply_to=ReplyTarget("G", "", True, None)
    ).to_dict()
    assert d2["reply_to"] == {"guid": "G", "text": "", "is_from_me": True, "sender_handle": None}
    d3 = make_message(reply_to_guid="G", reply_to=ReplyTarget("G", "", False, None)).to_dict()
    assert d3["reply_to"] == {"guid": "G", "text": "", "is_from_me": False, "sender_handle": None}
    # no presentation strings anywhere in the JSON
    for doc in (d, d2, d3):
        assert "You" not in json.dumps(doc) and "Attachment" not in json.dumps(doc)
    # unresolved reply: guid kept, reply_to None
    d4 = make_message(reply_to_guid="G", reply_to=None).to_dict()
    assert d4["reply_to_guid"] == "G"
    assert d4["reply_to"] is None


def test_to_dict_tapback_fields_and_service_raw() -> None:
    d = make_message(
        associated_type=2001,
        associated_guid="p:0/SYN-MSG-000000",
        service_raw="iMessageLite",
    ).to_dict()
    assert d["assoc_guid"] == "p:0/SYN-MSG-000000"
    assert d["assoc_type"] == 2001
    assert d["service"] == "iMessageLite"  # raw, not the Service enum or family
    assert make_message(service_raw=None).to_dict()["service"] is None


def test_to_dict_is_json_serialisable() -> None:
    m = make_message(
        has_attachments=True,
        attachments=(make_attachment(),),
        link=LinkPreview("https://example.invalid", "T", "S", "site", None, True),
        reply_to_guid="G",
        reply_to=ReplyTarget("G", "earlier", True, None),
        date_edited=DATE_NS + 1,
    )
    s = json.dumps(m.to_dict(), ensure_ascii=False)
    assert json.loads(s)["rowid"] == 1
    # nothing on the model leaks the enum repr
    assert "Service." not in s


def test_to_dict_does_not_mutate_or_share_state() -> None:
    m = make_message(has_attachments=True, attachments=(make_attachment(),))
    d1 = m.to_dict()
    d1["attachments"].append({"guid": "x"})
    d1["text"] = "changed"
    d2 = m.to_dict()
    assert len(d2["attachments"]) == 1
    assert d2["text"] == "hello there"


# ---------- Attachment ----------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("abc.pluginPayloadAttachment", True),
        ("IMG_0001.png", False),
        (None, False),
        ("", False),
        ("pluginPayloadAttachment", False),  # no dot: not the suffix
        ("x.PluginPayloadAttachment", False),  # case-sensitive like the relay
    ],
)
def test_attachment_is_plugin_payload(name: str | None, expected: bool) -> None:
    assert make_attachment(transfer_name=name).is_plugin_payload is expected


def test_attachment_path_expands_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    a = make_attachment(filename="~/synthetic/Attachments/x/IMG.png")
    p = a.path
    assert p == tmp_path / "synthetic/Attachments/x/IMG.png"
    assert p is not None and p.is_absolute()
    absolute = make_attachment(filename="/var/tmp/synthetic/IMG.png")
    assert absolute.path == Path("/var/tmp/synthetic/IMG.png")
    assert make_attachment(filename=None).path is None
    assert make_attachment(filename="").path is None
    # the raw column is untouched
    assert a.filename == "~/synthetic/Attachments/x/IMG.png"


def test_attachment_exists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "att" / "IMG.png"
    target.parent.mkdir()
    target.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert make_attachment(filename="~/att/IMG.png").exists() is True
    assert make_attachment(filename=str(target)).exists() is True
    assert make_attachment(filename="~/att/missing.png").exists() is False
    assert make_attachment(filename=None).exists() is False
    assert make_attachment(filename="").exists() is False


def test_attachment_hidden_not_plugin_keeps_both_facts() -> None:
    # the 20 hidden-not-plugin PNG rows: hide_attachment=1 but not a payload
    a = make_attachment(transfer_name="IMG_7.png", hide_attachment=True)
    assert a.hide_attachment is True
    assert a.is_plugin_payload is False
    unknown = make_attachment(hide_attachment=None, is_sticker=None, uti=None, total_bytes=None)
    assert unknown.hide_attachment is None and unknown.is_sticker is None


def test_attachment_message_date_unix() -> None:
    assert make_attachment(message_date=DATE_NS).message_date_unix == 1704067200.0
    assert make_attachment(message_date=0).message_date_unix is None
    assert make_attachment(message_date=None).message_date_unix is None


def test_attachment_to_dict() -> None:
    a = make_attachment()
    assert a.to_dict() == {
        "guid": a.guid,
        "mime_type": a.mime_type,
        "name": a.transfer_name,
        "filename": a.filename,
    }
    assert list(a.to_dict()) == ["guid", "mime_type", "name", "filename"]


# ---------- ReplyTarget / LiteMessage / ChatMatch ----------


def test_reply_target_to_dict() -> None:
    assert ReplyTarget("G", "hello", False, PHONE).to_dict() == {
        "guid": "G", "text": "hello", "is_from_me": False, "sender_handle": PHONE,
    }
    assert ReplyTarget("G", "hello", True, PHONE).to_dict() == {
        "guid": "G", "text": "hello", "is_from_me": True, "sender_handle": PHONE,
    }
    assert ReplyTarget("G", "", False, None).to_dict() == {
        "guid": "G", "text": "", "is_from_me": False, "sender_handle": None,
    }
    # never truncated, in the dict or on the model
    assert ReplyTarget("G", "y" * 121, False, None).to_dict()["text"] == "y" * 121
    assert len(ReplyTarget("G", "y" * 121, False, None).text) == 121
    assert json.dumps(ReplyTarget("G", "", True, None).to_dict())  # JSON-ready


def test_lite_message_and_chat_match_construct_positionally() -> None:
    att = make_attachment()
    lm = LiteMessage(5, "photo of the dog", False, 0, None, True, PHONE, (att,))
    assert lm.rowid == 5
    assert lm.attachments == (att,)
    assert lm.associated_type == 0
    cm = ChatMatch(3, "any;+;chat1", "chat1", "Team", True, 99)
    assert cm.is_group is True
    assert cm.last_rowid == 99
    assert cm == ChatMatch(
        chat_rowid=3,
        chat_guid="any;+;chat1",
        chat_identifier="chat1",
        display_name="Team",
        is_group=True,
        last_rowid=99,
    )


# ---------- Chat / ChatSummary ----------


@pytest.mark.parametrize(("style", "expected"), [(43, True), (45, False), (None, False)])
def test_chat_is_group(style: int | None, expected: bool) -> None:
    assert make_chat(style=style).is_group is expected
    assert make_summary(style=style).is_group is expected


def test_chat_name_and_to_dict() -> None:
    c = make_chat()
    assert c.chat_name == PHONE
    assert make_chat(display_name="Team", style=43).chat_name == "Team"
    d = c.to_dict()
    assert list(d) == [
        "rowid",
        "guid",
        "style",
        "chat_identifier",
        "display_name",
        "chat_name",
        "is_group",
        "service_name",
        "is_archived",
        "is_filtered",
        "group_id",
    ]
    assert d["is_group"] is False
    assert d["chat_name"] == PHONE
    assert d["service_name"] == "iMessage"
    json.dumps(d)


def test_chat_summary_to_dict_extends_chat() -> None:
    s = make_summary(style=43, guid="any;+;chat1", chat_identifier="chat1", display_name="Team")
    d = s.to_dict()
    assert list(d) == list(make_chat().to_dict()) + ["last_date", "last_rowid"]
    assert d["last_date"] == 1704067200.0 == legacy_apple_to_unix(DATE_NS)
    assert d["last_rowid"] == 7
    assert d["is_group"] is True
    assert d["chat_name"] == "Team"
    assert s.last_date_unix == 1704067200.0
    assert make_summary(last_date=None).to_dict()["last_date"] is None
    assert make_summary(last_date=0).last_date_unix is None
    # a ChatSummary is a Chat
    assert isinstance(s, Chat)
    assert Chat.to_dict(s) == {k: v for k, v in d.items() if k not in ("last_date", "last_rowid")}
    json.dumps(d)


def test_chat_optional_flags_may_be_none() -> None:
    c = make_chat(is_archived=None, is_filtered=None, group_id=None, service_name=None)
    d = c.to_dict()
    assert d["is_archived"] is None and d["is_filtered"] is None
    assert d["service_name"] is None


# ---------- SearchHit ----------


def test_search_hit_to_dict() -> None:
    h = make_hit()
    d = h.to_dict()
    assert list(d) == [
        "rowid",
        "chat_rowid",
        "chat_guid",
        "chat_identifier",
        "chat_display_name",
        "chat_name",
        "is_group",
        "date",
        "is_from_me",
        "sender_handle",
        "text",
        "match_index",
    ]
    assert d["date"] == 1704067200.0
    assert d["is_from_me"] is True
    assert d["sender_handle"] is None
    assert d["text"] == "meet at noon"
    assert d["match_index"] == 8
    assert d["chat_name"] == PHONE
    assert h.date_unix == 1704067200.0
    assert make_hit(chat_display_name="Team", is_group=True).to_dict()["chat_name"] == "Team"
    json.dumps(d)


# ---------- module surface ----------


def test_module_all_lists_every_model() -> None:
    for cls in ALL_MODELS:
        assert cls.__name__ in models.__all__
    assert "GROUP_STYLE" in models.__all__
    assert "PLUGIN_PAYLOAD_SUFFIX" in models.__all__
    for name in models.__all__:
        assert hasattr(models, name)
