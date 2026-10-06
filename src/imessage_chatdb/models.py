"""Data model: frozen, slotted dataclasses (DESIGN.md section 5).

Every class here is ``@dataclass(frozen=True, slots=True)``.  Values are kept
**raw** -- dates are Apple integers (nanoseconds since 2001-01-01, ``0`` means
"never"), handles are raw ``handle.id`` strings, services are the raw
``message.service`` text -- and the convenience views (``*_unix``, ``datetime``,
``service``, ``reaction``, ``clean_text``) are properties computed from the
leaf modules.  Booleans are real ``bool`` values; ``row_to_message`` (in
``messages.py``) applies ``bool()`` before constructing a ``Message``.

``to_dict()`` methods produce JSON-ready dicts for adopters and the CLI.
``Message.to_dict()`` uses the relay's key names in the relay's order but raw
values ("the relay's shape without the relay's strings"); the byte-identical
relay JSON is produced by the adapter inside the relay, not here.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .dates import apple_to_datetime, apple_to_unix
from .link_preview import LinkPreview
from .reactions import Reaction, classify_reaction
from .services import Service, normalize_service
from .typedstream import clean_text as _clean_text
from .typedstream_reader import AttributedBody, Part, message_parts, try_parse_attributed_body

__all__ = [
    "GROUP_STYLE",
    "PLUGIN_PAYLOAD_SUFFIX",
    "Message",
    "Attachment",
    "ReplyTarget",
    "LiteMessage",
    "Chat",
    "ChatSummary",
    "ChatMatch",
    "ChatActivity",
    "SearchHit",
]

#: ``chat.style`` value for a group chat (one-to-one chats are 45).
GROUP_STYLE = 43

#: ``attachment.transfer_name`` suffix of rich-link payload blobs that Messages
#: renders as cards rather than files (section 2; the default exclusion in
#: ``attachments_for``).  Duplicated from ``attachments.py`` because that module
#: imports this one.
PLUGIN_PAYLOAD_SUFFIX = ".pluginPayloadAttachment"


class _Unset:
    """Sentinel type for the not-yet-computed ``Message.body`` / ``.parts`` caches."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<unset>"


_UNSET = _Unset()


def _link_dict(link: LinkPreview | None) -> dict[str, Any] | None:
    """``LinkPreview`` as the relay's ``link`` dict shape with raw values.

    ``image`` is ``image_url``; when the preview only carries an embedded image
    blob (``has_embedded_image`` and no URL) it is ``None`` -- fetch the bytes
    with ``ChatDB.link_image(rowid)``.
    """
    if link is None:
        return None
    return {
        "url": link.url,
        "title": link.title,
        "summary": link.summary,
        "site": link.site,
        "image": link.image_url,
    }


@dataclass(frozen=True, slots=True)
class Attachment:
    """One ``attachment`` row joined to the message it belongs to.

    ``filename`` is the raw column (``~/``-prefixed, absolute, or ``NULL``);
    ``path`` expands it.  ``is_sticker`` / ``hide_attachment`` are ``None`` when
    the schema lacks the column.  ``message_date`` is the owning message's raw
    Apple date when the query carried it (``chat_attachments``), else ``None``.
    """

    message_rowid: int
    rowid: int
    guid: str
    mime_type: str | None
    transfer_name: str | None
    filename: str | None
    uti: str | None
    total_bytes: int | None
    is_sticker: bool | None
    hide_attachment: bool | None
    message_date: int | None

    @property
    def is_plugin_payload(self) -> bool:
        """True for rich-link payload blobs (``transfer_name`` suffix test only)."""
        return (self.transfer_name or "").endswith(PLUGIN_PAYLOAD_SUFFIX)

    @property
    def path(self) -> Path | None:
        """``filename`` with ``~`` expanded; ``None`` when ``filename`` is NULL or ``''``."""
        if not self.filename:
            return None
        return Path(self.filename).expanduser()

    @property
    def message_date_unix(self) -> float | None:
        """``message_date`` as Unix seconds (``None`` for ``0``/``None``)."""
        return apple_to_unix(self.message_date)

    def exists(self) -> bool:
        """True when ``path`` is set and exists on disk."""
        p = self.path
        return p is not None and p.exists()

    def to_dict(self) -> dict[str, Any]:
        """Relay attachment shape without the URL: ``guid, mime_type, name, filename``.

        ``name`` is the raw ``transfer_name``; there is no ``url`` key because
        serving attachments is a caller concern.
        """
        return {
            "guid": self.guid,
            "mime_type": self.mime_type,
            "name": self.transfer_name,
            "filename": self.filename,
        }


@dataclass(frozen=True, slots=True)
class ReplyTarget:
    """The message a reply quotes (``thread_originator_guid`` target).

    ``text`` is ``clean_text(effective_text(...))``: whitespace-collapsed,
    U+FFFC removed, **untruncated**, and ``''`` when the target has no text
    (attachment-only).  Fallback strings and truncation belong to callers.
    """

    guid: str
    text: str
    is_from_me: bool
    sender_handle: str | None

    def to_dict(self) -> dict[str, Any]:
        """Facts only: ``{"guid", "text", "is_from_me", "sender_handle"}``.

        ``text`` is the cleaned, **untruncated** text (``''`` for an
        attachment-only target) and ``sender_handle`` the raw handle (``None``
        for outgoing).  No ``"You"``, no ``"Attachment"``, no ``[:120]`` -- those
        are the relay adapter's strings (section 5.1), built on top of these
        facts, never emitted here.
        """
        return {
            "guid": self.guid,
            "text": self.text,
            "is_from_me": self.is_from_me,
            "sender_handle": self.sender_handle,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class Message:
    """One ``message`` row with its chat, sender and optional enrichment.

    Fields mirror the message SELECT (section 6.1).  Optional columns missing
    from the schema arrive as ``None``.  ``text`` is
    ``effective_text(text_column, attributed_body)``; ``text_column`` is the raw
    ``m.text``.  ``attachments`` and ``reply_to`` are filled by ``enrich`` (then
    ``enriched`` is True); ``link`` is parsed only when ``balloon_bundle_id`` is
    ``LINK_BALLOON``.

    ``chat_rowid`` and ``chat_guid`` are ``None`` only on rows returned by
    ``messages_after(..., include_orphans=True)`` (a ``message`` row with no
    ``chat_message_join`` row); every other fetch inner-joins the chat.

    ``attributed_body_raw`` (0.2) is the raw ``attributedBody`` blob the row
    carried (``None`` when NULL); ``body`` parses it with the full typedstream
    reader on first access and ``parts`` splits the result into text and
    attachment parts.  Both are cached on the instance and never raise: a
    blob the reader rejects gives ``body is None`` and ``parts == ()`` while
    ``text`` (the byte-scan) may still be set.  ``body.text`` differs from
    ``text`` only when ``text_column`` is non-empty, because the column wins
    in ``effective_text``.  ``to_dict()`` is unchanged: no new key.

    Keyword-only: with ~40 fields positional construction is never intended.
    """

    rowid: int
    guid: str
    text: str | None
    text_column: str | None
    date: int
    date_read: int | None
    date_delivered: int | None
    date_edited: int | None
    date_retracted: int | None
    is_from_me: bool
    handle_rowid: int | None
    sender_handle: str | None
    chat_rowid: int | None
    chat_guid: str | None
    chat_identifier: str | None
    chat_display_name: str | None
    chat_style: int | None
    service_raw: str | None
    has_attachments: bool
    attachments: tuple[Attachment, ...] = ()
    associated_guid: str | None
    associated_type: int | None
    associated_emoji: str | None
    reply_to_guid: str | None
    reply_to_part: str | None
    reply_to: ReplyTarget | None = None
    balloon_bundle_id: str | None
    link: LinkPreview | None
    item_type: int | None
    group_action_type: int | None
    group_title: str | None
    filter_action: int | None
    is_spam: bool | None
    expressive_send_style_id: str | None
    enriched: bool = False
    attributed_body_raw: bytes | None = None
    # Per-instance caches for ``body`` / ``parts``.  ``functools.cached_property``
    # cannot be used on a frozen, slotted dataclass, so these are private slots
    # written with ``object.__setattr__``; ``init=False`` keeps them out of the
    # constructor and ``dataclasses.replace`` (which resets them), and
    # ``compare=False`` / ``repr=False`` keep them out of ``==`` and ``repr``.
    _body_cache: AttributedBody | None | _Unset = field(
        default=_UNSET, init=False, repr=False, compare=False
    )
    _parts_cache: tuple[Part, ...] | _Unset = field(
        default=_UNSET, init=False, repr=False, compare=False
    )

    # ----- derived views -----

    @property
    def body(self) -> AttributedBody | None:
        """``attributed_body_raw`` parsed by the typedstream reader; ``None`` if it cannot be.

        Computed on first access and cached; never raises.
        """
        cached = self._body_cache
        if isinstance(cached, _Unset):
            cached = try_parse_attributed_body(self.attributed_body_raw)
            object.__setattr__(self, "_body_cache", cached)
        return cached

    @property
    def parts(self) -> tuple[Part, ...]:
        """``message_parts(body)``, or ``()`` when ``body`` is ``None``.  Cached."""
        cached = self._parts_cache
        if isinstance(cached, _Unset):
            body = self.body
            cached = () if body is None else message_parts(body)
            object.__setattr__(self, "_parts_cache", cached)
        return cached

    @property
    def is_group(self) -> bool:
        """True when the chat's ``style`` is 43 (group); 45 is one-to-one."""
        return self.chat_style == GROUP_STYLE

    @property
    def service(self) -> Service:
        """``service_raw`` normalised to a ``Service`` (``UNKNOWN`` for ``None``/odd values)."""
        return normalize_service(self.service_raw)

    @property
    def reaction(self) -> Reaction | None:
        """Tapback / sticker classification; ``None`` for ordinary messages."""
        return classify_reaction(self.associated_type, self.associated_guid, self.associated_emoji)

    @property
    def is_tapback(self) -> bool:
        """True when ``reaction`` is not ``None`` (add/remove tapbacks and stickers)."""
        return self.reaction is not None

    @property
    def date_unix(self) -> float | None:
        return apple_to_unix(self.date)

    @property
    def date_read_unix(self) -> float | None:
        return apple_to_unix(self.date_read)

    @property
    def date_delivered_unix(self) -> float | None:
        return apple_to_unix(self.date_delivered)

    @property
    def date_edited_unix(self) -> float | None:
        return apple_to_unix(self.date_edited)

    @property
    def date_retracted_unix(self) -> float | None:
        return apple_to_unix(self.date_retracted)

    @property
    def datetime(self) -> dt.datetime | None:
        """``date`` as a timezone-aware UTC ``datetime`` (``None`` when ``date`` is 0)."""
        return apple_to_datetime(self.date)

    @property
    def clean_text(self) -> str:
        """``text`` with U+FFFC removed and whitespace collapsed; ``''`` for ``None``."""
        return _clean_text(self.text)

    @property
    def chat_name(self) -> str | None:
        """``chat_display_name or chat_identifier`` (no contact resolution)."""
        return self.chat_display_name or self.chat_identifier

    def to_dict(self) -> dict[str, Any]:
        """Relay message keys in relay order, raw values (section 5.1).

        Key order: ``rowid, guid, text, date, date_read, date_edited, is_from_me,
        sender, sender_handle, chat_guid, chat_name, is_group, has_attachments,
        assoc_guid, assoc_type, attachments, link, reply_to_guid, reply_to,
        service``.  Dates are ``apple_to_unix`` floats (``None`` for 0/NULL);
        ``sender`` is the raw handle (same as ``sender_handle``); ``chat_name``
        is ``display_name or chat_identifier``; attachments are
        ``Attachment.to_dict()`` (no ``url``); ``link.image`` is ``image_url``;
        ``reply_to`` is ``ReplyTarget.to_dict()`` (``guid, text, is_from_me,
        sender_handle``: facts, no fallback strings); ``service`` is
        ``service_raw``.
        """
        return {
            "rowid": self.rowid,
            "guid": self.guid,
            "text": self.text,
            "date": apple_to_unix(self.date),
            "date_read": apple_to_unix(self.date_read),
            "date_edited": apple_to_unix(self.date_edited),
            "is_from_me": self.is_from_me,
            "sender": self.sender_handle,
            "sender_handle": self.sender_handle,
            "chat_guid": self.chat_guid,
            "chat_name": self.chat_name,
            "is_group": self.is_group,
            "has_attachments": self.has_attachments,
            "assoc_guid": self.associated_guid,
            "assoc_type": self.associated_type,
            "attachments": [a.to_dict() for a in self.attachments],
            "link": _link_dict(self.link),
            "reply_to_guid": self.reply_to_guid,
            "reply_to": None if self.reply_to is None else self.reply_to.to_dict(),
            "service": self.service_raw,
        }


@dataclass(frozen=True, slots=True)
class LiteMessage:
    """Facts for a chat's last-message preview (``lite_messages``).

    ``text`` is cleaned and ``''`` when the row has none; ``attachments`` is
    filled only for ``has_attachments`` rows.  Preview strings (verbs, "Photo",
    paperclips) are the caller's.
    """

    rowid: int
    text: str
    is_from_me: bool
    associated_type: int
    associated_emoji: str | None
    has_attachments: bool
    sender_handle: str | None
    attachments: tuple[Attachment, ...]


@dataclass(frozen=True, slots=True)
class Chat:
    """One ``chat`` row.  ``style`` 43 = group, 45 = one-to-one."""

    rowid: int
    guid: str
    style: int | None
    chat_identifier: str | None
    display_name: str | None
    service_name: str | None
    is_archived: bool | None
    is_filtered: bool | None
    group_id: str | None

    @property
    def is_group(self) -> bool:
        """True when ``style`` is 43."""
        return self.style == GROUP_STYLE

    @property
    def chat_name(self) -> str | None:
        """``display_name or chat_identifier`` (no contact resolution)."""
        return self.display_name or self.chat_identifier

    def to_dict(self) -> dict[str, Any]:
        """Keys: ``rowid, guid, style, chat_identifier, display_name, chat_name,
        is_group, service_name, is_archived, is_filtered, group_id``."""
        return {
            "rowid": self.rowid,
            "guid": self.guid,
            "style": self.style,
            "chat_identifier": self.chat_identifier,
            "display_name": self.display_name,
            "chat_name": self.chat_name,
            "is_group": self.is_group,
            "service_name": self.service_name,
            "is_archived": self.is_archived,
            "is_filtered": self.is_filtered,
            "group_id": self.group_id,
        }


@dataclass(frozen=True, slots=True)
class ChatSummary(Chat):
    """A ``Chat`` plus its latest activity (``chats_by_activity``).

    ``last_date`` is the raw Apple date of the newest message (``None`` when
    unknown); ``last_rowid`` is that message's ROWID.
    """

    last_date: int | None
    last_rowid: int

    @property
    def last_date_unix(self) -> float | None:
        return apple_to_unix(self.last_date)

    def to_dict(self) -> dict[str, Any]:
        """``Chat.to_dict()`` keys followed by ``last_date`` (Unix seconds or
        ``None``) and ``last_rowid``."""
        d = Chat.to_dict(self)
        d["last_date"] = apple_to_unix(self.last_date)
        d["last_rowid"] = self.last_rowid
        return d


@dataclass(frozen=True, slots=True)
class ChatMatch:
    """Result of ``find_chat``: the chat whose participants match the addresses."""

    chat_rowid: int
    chat_guid: str
    chat_identifier: str | None
    display_name: str | None
    is_group: bool
    last_rowid: int


@dataclass(frozen=True, slots=True)
class ChatActivity:
    """One chat that gained messages (``chats_changed_since``).

    ``chat_rowid`` is the ``chat.ROWID``; ``last_rowid`` is the newest
    ``message.ROWID`` joined to it -- the same value ``ChatSummary.last_rowid``
    carries, so a cached chat list can be refreshed in place.
    """

    chat_rowid: int
    last_rowid: int

    def to_dict(self) -> dict[str, Any]:
        """Keys: ``chat_rowid, last_rowid``."""
        return {"chat_rowid": self.chat_rowid, "last_rowid": self.last_rowid}


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One ``search`` result.

    ``text`` is the cleaned effective text (``clean_text(effective_text(...))``)
    and ``match_index`` is the index of the case-insensitive match within it.
    """

    rowid: int
    chat_rowid: int
    chat_guid: str
    chat_identifier: str | None
    chat_display_name: str | None
    is_group: bool
    date: int
    is_from_me: bool
    sender_handle: str | None
    text: str
    match_index: int

    @property
    def date_unix(self) -> float | None:
        return apple_to_unix(self.date)

    @property
    def chat_name(self) -> str | None:
        """``chat_display_name or chat_identifier`` (no contact resolution)."""
        return self.chat_display_name or self.chat_identifier

    def to_dict(self) -> dict[str, Any]:
        """Keys: ``rowid, chat_rowid, chat_guid, chat_identifier, chat_display_name,
        chat_name, is_group, date, is_from_me, sender_handle, text, match_index``
        (``date`` as Unix seconds)."""
        return {
            "rowid": self.rowid,
            "chat_rowid": self.chat_rowid,
            "chat_guid": self.chat_guid,
            "chat_identifier": self.chat_identifier,
            "chat_display_name": self.chat_display_name,
            "chat_name": self.chat_name,
            "is_group": self.is_group,
            "date": apple_to_unix(self.date),
            "is_from_me": self.is_from_me,
            "sender_handle": self.sender_handle,
            "text": self.text,
            "match_index": self.match_index,
        }
