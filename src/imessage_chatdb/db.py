"""The ``ChatDB`` facade and ``open()`` (DESIGN.md sections 4.1, 4.5).

``ChatDB`` holds a path and some options - never a connection.  Every public
method opens a fresh read-only connection (:func:`open_connection`, ~0.3 ms),
runs the matching module function with the documented defaults, and closes
the connection in ``finally``; that keeps the WAL free of lingering read
snapshots.  :meth:`ChatDB.connection` batches
several calls on one connection for callers who want a consistent view.

The :class:`Schema` is introspected once, lazily, on first use and cached on
the instance; :meth:`refresh_schema` rebuilds it.  Connections are never
shared across threads (``check_same_thread`` stays at its default); the batch
connection is thread-local, so ``asyncio.to_thread(db.poll, cursor)`` works.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from . import attachments as _attachments
from . import chats as _chats
from . import messages as _messages
from . import search as _search
from .connection import DEFAULT_CHATDB, open_connection, translate_sqlite_error
from .errors import ChatDBBusy
from .handles import address_key
from .link_preview import embedded_image
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
from .polling import Cursor, Event, poll_once
from .polling import watch as _watch
from .schema import Schema

__all__ = ["ChatDB", "open"]


class ChatDB:
    """Read-only access to one ``chat.db`` file; one connection per call.

    ``path`` is stored expanded (``~`` resolved) as a :class:`pathlib.Path`.
    ``timeout`` is SQLite's busy timeout in seconds.  ``readonly_uri=False`` is
    the compatibility shim described on :func:`open_connection`, not a feature.
    Nothing is opened in ``__init__``; the first call that needs the database
    opens it, so access errors surface from that call (or from :func:`open`).
    """

    __slots__ = ("_local", "_schema", "path", "readonly_uri", "timeout")

    path: Path
    timeout: float
    readonly_uri: bool

    def __init__(
        self,
        path: str | os.PathLike[str] = DEFAULT_CHATDB,
        *,
        timeout: float = 5.0,
        readonly_uri: bool = True,
    ) -> None:
        self.path = Path(os.fspath(path)).expanduser()
        self.timeout = timeout
        self.readonly_uri = readonly_uri
        self._schema: Schema | None = None
        self._local = threading.local()

    def __repr__(self) -> str:
        return f"ChatDB({str(self.path)!r})"

    # -- connections --------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        """A **new** read-only connection every call; the caller closes it."""
        return open_connection(self.path, timeout=self.timeout, readonly_uri=self.readonly_uri)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Batch several facade calls on one connection.

        Inside the ``with`` block every method of this ``ChatDB`` (on this
        thread) reuses the yielded connection instead of opening its own; it is
        closed when the block ends.  Nesting reuses the outer connection.  A
        ``sqlite3`` "locked"/"busy" error raised inside the block becomes
        :class:`ChatDBBusy`, as it would at open time.
        """
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            yield existing
            return
        conn = self.connect()
        self._local.conn = conn
        try:
            yield conn
        except sqlite3.Error as exc:
            translated = translate_sqlite_error(exc)
            if isinstance(translated, ChatDBBusy):
                raise translated from exc
            raise
        finally:
            self._local.conn = None
            conn.close()

    def close(self) -> None:
        """No-op placeholder: a ``ChatDB`` holds no connection between calls."""

    # -- schema -------------------------------------------------------------

    @property
    def schema(self) -> Schema:
        """The database's :class:`Schema`, introspected on first access and cached.

        Raises :class:`SchemaError` (via :meth:`refresh_schema`) when the file
        is not a Messages database.
        """
        schema = self._schema
        if schema is None:
            schema = self.refresh_schema()
        return schema

    def refresh_schema(self) -> Schema:
        """Re-introspect the schema (after a macOS upgrade, say) and return it.

        The new :class:`Schema` is checked (:meth:`Schema.check`) before it
        replaces the cached one, so a file that lacks a required column - a
        SQLite database that is not ``chat.db`` - raises
        :class:`SchemaError` here, the library's own error, instead of a raw
        ``sqlite3.OperationalError`` from the first query.  The cache is left
        untouched when the check fails.
        """
        with self.connection() as conn:
            schema = Schema(conn)
        schema.check()
        self._schema = schema
        return schema

    # -- cursors ------------------------------------------------------------

    def max_rowid(self) -> int:
        with self.connection() as conn:
            return _messages.max_rowid(conn)

    def max_date_edited(self) -> int:
        with self.connection() as conn:
            return _messages.max_date_edited(conn, self.schema)

    def initial_cursor(self) -> Cursor:
        """``Cursor(max_rowid(), max_date_edited())`` - "now", read on one connection."""
        with self.connection() as conn:
            return Cursor(
                rowid=_messages.max_rowid(conn),
                edit_mark=_messages.max_date_edited(conn, self.schema),
            )

    # -- messages -----------------------------------------------------------

    def messages_after(
        self,
        rowid: int,
        *,
        limit: int | None = None,
        enrich: bool = True,
        include_orphans: bool = False,
    ) -> list[Message]:
        with self.connection() as conn:
            return _messages.messages_after(
                conn, self.schema, rowid, limit=limit, enrich=enrich,
                include_orphans=include_orphans,
            )

    def messages_edited_after(self, mark: int, *, enrich: bool = True) -> tuple[list[Message], int]:
        with self.connection() as conn:
            return _messages.messages_edited_after(conn, self.schema, mark, enrich=enrich)

    def thread_messages(
        self,
        chat_guid: str,
        *,
        limit: int = 50,
        before_rowid: int | None = None,
        enrich: bool = True,
    ) -> list[Message]:
        with self.connection() as conn:
            return _messages.thread_messages(
                conn, self.schema, chat_guid, limit=limit, before_rowid=before_rowid, enrich=enrich
            )

    def recent_messages(
        self, chat_guid: str, *, limit: int = 1000, enrich: bool = False
    ) -> list[Message]:
        with self.connection() as conn:
            return _messages.recent_messages(
                conn, self.schema, chat_guid, limit=limit, enrich=enrich
            )

    def message(self, rowid: int, *, enrich: bool = True) -> Message | None:
        with self.connection() as conn:
            return _messages.message(conn, self.schema, rowid, enrich=enrich)

    def messages_by_guid(self, guids: Iterable[str], *, enrich: bool = True) -> dict[str, Message]:
        with self.connection() as conn:
            return _messages.messages_by_guid(conn, self.schema, guids, enrich=enrich)

    def reply_targets(self, guids: Iterable[str]) -> dict[str, ReplyTarget]:
        with self.connection() as conn:
            return _messages.reply_targets(conn, guids)

    # -- attachments --------------------------------------------------------

    def attachments_for(
        self, rowids: Iterable[int], *, include_plugin_payloads: bool = False
    ) -> dict[int, list[Attachment]]:
        with self.connection() as conn:
            return _attachments.attachments_for(
                conn, rowids, include_plugin_payloads=include_plugin_payloads
            )

    def attachment(self, guid: str) -> Attachment | None:
        with self.connection() as conn:
            return _attachments.attachment_by_guid(conn, guid)

    def chat_attachments(
        self, chat_guid: str, *, include_plugin_payloads: bool = False
    ) -> list[Attachment]:
        with self.connection() as conn:
            return _attachments.chat_attachments(
                conn, chat_guid, include_plugin_payloads=include_plugin_payloads
            )

    def payload(self, rowid: int) -> bytes | None:
        with self.connection() as conn:
            return _attachments.payload_for(conn, rowid)

    def link_image(self, rowid: int) -> bytes | None:
        """``embedded_image(payload(rowid))``: the largest image blob in the link archive.

        ``None`` covers "no row", "no payload", "unparseable payload" and "no
        image" alike; a caller that must tell them apart uses
        :meth:`payload` and :func:`imessage_chatdb.embedded_images` directly.
        """
        return embedded_image(self.payload(rowid))

    # -- chats --------------------------------------------------------------

    def chats(self, *, limit: int = 200) -> list[ChatSummary]:
        """Chats by recent activity (``chats_by_activity``)."""
        with self.connection() as conn:
            return _chats.chats_by_activity(conn, limit=limit)

    def chat(self, guid: str) -> Chat | None:
        with self.connection() as conn:
            return _chats.chat(conn, guid)

    def chat_by_rowid(self, rowid: int) -> Chat | None:
        with self.connection() as conn:
            return _chats.chat_by_rowid(conn, rowid)

    def participants(self, chat_rowid: int) -> list[str]:
        with self.connection() as conn:
            return _chats.participants(conn, chat_rowid)

    def participants_map(self) -> dict[int, list[str]]:
        with self.connection() as conn:
            return _chats.participants_map(conn)

    def chat_services(self, chat_rowids: Iterable[int]) -> dict[int, str | None]:
        with self.connection() as conn:
            return _chats.chat_services(conn, chat_rowids)

    def last_rowid_for(self, chat_rowid: int) -> int:
        with self.connection() as conn:
            return _chats.last_rowid_for(conn, chat_rowid)

    def unread_count(self, chat_rowid: int, after_rowid: int, *, incoming_only: bool = True) -> int:
        with self.connection() as conn:
            return _chats.unread_count(conn, chat_rowid, after_rowid, incoming_only=incoming_only)

    def lite_messages(self, rowids: Iterable[int]) -> dict[int, LiteMessage]:
        with self.connection() as conn:
            return _chats.lite_messages(conn, rowids)

    def one_to_one_activity(self) -> list[tuple[str, int]]:
        with self.connection() as conn:
            return _chats.one_to_one_activity(conn)

    def find_chat(
        self,
        addresses: Sequence[str],
        *,
        key: Callable[[str], str | None] = address_key,
        exclude: Iterable[str] = (),
    ) -> ChatMatch | None:
        with self.connection() as conn:
            return _chats.find_chat(conn, addresses, key=key, exclude=exclude)

    # -- search -------------------------------------------------------------

    def search(
        self, q: str, *, limit: int = 30, chat_guid: str | None = None
    ) -> list[SearchHit]:
        """:func:`imessage_chatdb.search.search`; ``chat_guid`` scopes it to one chat."""
        with self.connection() as conn:
            return _search.search(conn, q, limit=limit, chat_guid=chat_guid)

    # -- watching -----------------------------------------------------------

    def poll(self, cursor: Cursor, *, include_edits: bool = True) -> tuple[list[Event], Cursor]:
        """One polling round (:func:`imessage_chatdb.polling.poll_once`)."""
        return poll_once(self, cursor, include_edits=include_edits)

    def watch(
        self,
        cursor: Cursor | None = None,
        *,
        interval: float = 2.0,
        stop: threading.Event | None = None,
        include_edits: bool = True,
        sleep: Callable[[float], object] = time.sleep,
    ) -> Iterator[Event]:
        """Yield events forever (:func:`imessage_chatdb.polling.watch`)."""
        return _watch(
            self, cursor, interval=interval, stop=stop, include_edits=include_edits, sleep=sleep
        )


def open(path: str | os.PathLike[str] = DEFAULT_CHATDB, *, timeout: float = 5.0) -> ChatDB:
    """Open ``path`` read-only and return a :class:`ChatDB`.

    Unlike ``ChatDB(...)``, this probes the database once (introspecting,
    checking and caching the schema), so a missing Full Disk Access grant
    raises :class:`ChatDBAccessError` here and a SQLite file that is not a
    Messages database raises :class:`SchemaError` here, rather than from the
    first query.
    """
    db = ChatDB(path, timeout=timeout)
    db.refresh_schema()
    return db
