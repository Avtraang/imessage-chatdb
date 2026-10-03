"""Exception hierarchy (DESIGN.md section 6.6).

``ChatDBError`` is the base for everything the library raises deliberately.
Decoders (``extract_text``, ``parse_link_preview``, ...) never raise; these
classes are for opening, locking, cursor and schema problems.
"""

from __future__ import annotations

import sys

__all__ = [
    "ChatDBError",
    "ChatDBAccessError",
    "ChatDBBusy",
    "CursorAhead",
    "SchemaError",
]

FULL_DISK_ACCESS_HINT = (
    "Full Disk Access is granted per interpreter binary. Add this exact binary "
    "in System Settings > Privacy & Security > Full Disk Access: {executable}. "
    "Note: upgrading the interpreter (for example `brew upgrade python`) installs a new "
    "binary and silently orphans the grant; re-add the new path."
)


class ChatDBError(Exception):
    """Base class for all errors raised by imessage-chatdb."""


class ChatDBAccessError(ChatDBError):
    """The database could not be opened for reading.

    Raised for SQLite's "unable to open database file", "authorization denied"
    and "not a database" conditions.  The message has one shape in every open
    mode::

        <path>: <sqlite message>. <Full Disk Access advice>

    where the advice names ``sys.executable`` and the Settings pane, so a caller
    can fix the grant without guessing.  ``reason`` is the bare SQLite message,
    ``path`` the absolute path that failed (``None`` when the error was built
    without one, in which case the message starts at the reason) and
    ``executable`` the interpreter the advice names.
    """

    def __init__(
        self, reason: str, *, path: str | None = None, executable: str | None = None
    ) -> None:
        exe = sys.executable if executable is None else executable
        self.reason = reason
        self.path = path
        self.executable = exe
        where = "" if path is None else f"{path}: "
        super().__init__(f"{where}{reason}. {FULL_DISK_ACCESS_HINT.format(executable=exe)}")


class ChatDBBusy(ChatDBError):
    """The database is locked or busy; the operation is safe to retry."""


class CursorAhead(ChatDBError):
    """The caller's cursor ROWID is beyond ``MAX(ROWID)`` (the database was rebuilt).

    The library never resolves this on its own; the caller decides whether to
    re-initialise at ``max_rowid`` or replay from zero.
    """

    def __init__(self, cursor_rowid: int, max_rowid: int) -> None:
        self.cursor_rowid = cursor_rowid
        self.max_rowid = max_rowid
        super().__init__(
            f"cursor rowid {cursor_rowid} is ahead of MAX(ROWID) {max_rowid}; "
            "the database was probably rebuilt"
        )


class SchemaError(ChatDBError):
    """A required column is missing; ``column`` is ``"table.column"``."""

    def __init__(self, column: str) -> None:
        self.column = column
        super().__init__(f"required column missing: {column}")
