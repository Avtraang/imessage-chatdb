"""imessage-chatdb: a read-only, stdlib-only reader for Apple Messages ``chat.db``.

The public surface of DESIGN.md section 4: :func:`open` / :class:`ChatDB`
(section 4.1, 4.5), the schema (4.2), the pure functions and constants (4.3),
the models (5), the errors (6.6) and the polling primitives (4.6).  The query
functions that take a connection (section 4.4) live on their submodules
(``imessage_chatdb.messages``, ``.attachments``, ``.chats``, ``.search``) and
behind the same names on :class:`ChatDB`.

One name is deliberately kept off ``__all__``: ``imessage_chatdb.open``
exists (``imessage_chatdb.open(path)``) but is not exported, so
``from imessage_chatdb import *`` never shadows the builtin.  The polling
primitives live in ``imessage_chatdb.polling`` and are re-exported here, so
``imessage_chatdb.watch`` *is* the ``watch()`` generator (also reachable as
:meth:`ChatDB.watch`).

Nothing here opens a database at import time.
"""

from __future__ import annotations

from .connection import DEFAULT_CHATDB, open_connection
from .dates import APPLE_EPOCH_OFFSET, apple_to_datetime, apple_to_unix, unix_to_apple
from .db import ChatDB
from .db import open as open
from .errors import ChatDBAccessError, ChatDBBusy, ChatDBError, CursorAhead, SchemaError
from .handles import address_key, is_email, is_group_style
from .keyed_archive import KeyedArchive
from .link_preview import (
    LINK_BALLOON,
    SKIP_IMG,
    LinkPreview,
    embedded_image,
    embedded_images,
    parse_link_preview,
    sniff_image_mime,
)
from .models import (
    Attachment,
    Chat,
    ChatMatch,
    ChatSummary,
    LiteMessage,
    Message,
    ReplyTarget,
    SearchHit,
)
from .polling import Cursor, Event, poll_once, run_watch, watch
from .reactions import Reaction, classify_reaction, parse_associated_guid
from .schema import OPTIONAL, REQUIRED, Schema, build_message_select
from .search import extract_urls, snippet
from .services import Service, normalize_service, service_family
from .typedstream import clean_text, effective_text, extract_text

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # errors
    "ChatDBError",
    "ChatDBAccessError",
    "ChatDBBusy",
    "CursorAhead",
    "SchemaError",
    # opening (``open`` is an attribute, not an export: see the module docstring)
    "DEFAULT_CHATDB",
    "open_connection",
    "ChatDB",
    # schema
    "Schema",
    "REQUIRED",
    "OPTIONAL",
    "build_message_select",
    # dates
    "APPLE_EPOCH_OFFSET",
    "apple_to_unix",
    "unix_to_apple",
    "apple_to_datetime",
    # typedstream
    "extract_text",
    "effective_text",
    "clean_text",
    # keyed archive
    "KeyedArchive",
    # link preview
    "LINK_BALLOON",
    "SKIP_IMG",
    "LinkPreview",
    "parse_link_preview",
    "embedded_images",
    "embedded_image",
    "sniff_image_mime",
    # reactions
    "Reaction",
    "classify_reaction",
    "parse_associated_guid",
    # services
    "Service",
    "normalize_service",
    "service_family",
    # handles
    "address_key",
    "is_email",
    "is_group_style",
    # search helpers
    "extract_urls",
    "snippet",
    # models
    "Message",
    "Attachment",
    "ReplyTarget",
    "LiteMessage",
    "Chat",
    "ChatSummary",
    "ChatMatch",
    "SearchHit",
    # polling (``imessage_chatdb.polling``)
    "Cursor",
    "Event",
    "poll_once",
    "watch",
    "run_watch",
]
