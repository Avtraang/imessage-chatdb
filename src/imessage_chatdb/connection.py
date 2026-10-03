"""Opening ``chat.db`` read-only (DESIGN.md section 4.1).

Invariants every connection returned here satisfies:

- read-only: ``?mode=ro`` URI (default) **and** ``PRAGMA query_only = 1``;
- autocommit (``isolation_level=None``) so no read snapshot lingers in the WAL;
- ``row_factory = sqlite3.Row``;
- never ``immutable=1`` - it ignores the WAL and misses fresh rows;
- SQLite is the first thing to touch the path: no ``os.path.exists`` / ``stat``
  probe precedes ``sqlite3.connect`` in either mode, so a wrapper around
  ``sqlite3.connect`` (the test-suite's ``~/Library`` guard) sees every open
  before any filesystem access.

Errors are translated at open time: Full Disk Access problems become
``ChatDBAccessError`` (``"<path>: <sqlite message>. <advice naming
sys.executable and the Settings pane>"``), lock contention becomes
``ChatDBBusy``; anything else propagates untouched.
"""

from __future__ import annotations

import os
import sqlite3
from urllib.parse import quote

from .errors import ChatDBAccessError, ChatDBBusy, ChatDBError

__all__ = [
    "DEFAULT_CHATDB",
    "open_connection",
    "uri_for_path",
    "translate_sqlite_error",
]

DEFAULT_CHATDB = "~/Library/Messages/chat.db"

_PROBE = "SELECT 1 FROM sqlite_master LIMIT 1"

# Substrings of sqlite3 error messages that mean "you cannot read this file".
_ACCESS_MARKERS = ("unable to open database file", "authorization denied", "not a database")
_BUSY_MARKERS = ("locked", "busy")


def _abspath(path: str | os.PathLike[str]) -> str:
    return os.path.abspath(os.path.expanduser(os.fspath(path)))


def _uri(abspath: str, mode: str) -> str:
    """``file:`` URI for an absolute path, percent-encoded, with ``?mode=<mode>``."""
    return f"file:{quote(abspath)}?mode={mode}"


def uri_for_path(path: str | os.PathLike[str]) -> str:
    """The ``file:`` URI used for ``readonly_uri=True``.

    The absolute path is percent-encoded (``quote`` with ``/`` kept), so a space
    becomes ``%20`` and a literal ``%`` becomes ``%25``; otherwise SQLite would
    read ``%`` and ``?`` as URI syntax.
    """
    return _uri(_abspath(path), "ro")


def translate_sqlite_error(exc: sqlite3.Error, *, path: str | None = None) -> ChatDBError | None:
    """Map a ``sqlite3`` error to the library's error, or ``None`` if it is neither.

    - "database is locked" / "busy" -> :class:`ChatDBBusy` (retryable)
    - "unable to open database file" / "authorization denied" /
      "file is not a database" -> :class:`ChatDBAccessError`, whose message
      starts with ``path`` when one is given (``open_connection`` always passes
      the absolute path it tried).

    The returned exception has ``exc`` attached as ``__cause__`` when raised with
    ``raise translated from exc``; this function only builds it.
    """
    msg = str(exc).lower()
    if any(marker in msg for marker in _BUSY_MARKERS):
        return ChatDBBusy(str(exc))
    if any(marker in msg for marker in _ACCESS_MARKERS):
        return ChatDBAccessError(str(exc), path=path)
    return None


def open_connection(
    path: str | os.PathLike[str] = DEFAULT_CHATDB,
    *,
    timeout: float = 5.0,
    readonly_uri: bool = True,
) -> sqlite3.Connection:
    """Open ``path`` for reading and return an autocommit, ``sqlite3.Row`` connection.

    ``readonly_uri=True`` (default, the only documented mode)::

        sqlite3.connect(f"file:{quote(abspath)}?mode=ro", uri=True,
                        timeout=timeout, isolation_level=None)
        PRAGMA query_only = 1
        SELECT 1 FROM sqlite_master LIMIT 1   -- forces the open so FDA errors surface here

    ``readonly_uri=False`` is a compatibility shim for the relay's historical
    ``sqlite3.connect(path, timeout)`` + ``PRAGMA query_only = 1``.  It is not a
    feature: the connection is read-write at the SQLite level (``query_only``
    is what stops writes), so SQLite may create ``-wal``/``-shm`` files next to
    the database.  It opens ``file:{quote(abspath)}?mode=rw`` -- the plain
    ``connect``'s flags minus ``SQLITE_OPEN_CREATE`` -- so a missing file is an
    error rather than a freshly created empty database, and SQLite itself is
    the first thing to touch the path in both modes (no ``os.path.exists``
    probe).  Neither mode ever uses ``immutable=1``.

    Raises ``ChatDBAccessError`` -- ``"<path>: <sqlite message>. <Full Disk
    Access advice naming sys.executable and the Settings pane>"``, the same
    shape in both modes -- for "unable to open database file", "authorization
    denied" and "not a database"; ``ChatDBBusy`` for "locked"/"busy"; other
    ``sqlite3`` errors propagate.
    """
    abspath = _abspath(path)
    mode = "ro" if readonly_uri else "rw"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(_uri(abspath, mode), uri=True, timeout=timeout, isolation_level=None)
        conn.execute("PRAGMA query_only = 1").fetchall()
        conn.execute(_PROBE).fetchall()
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        if conn is not None:
            conn.close()
        translated = translate_sqlite_error(exc, path=abspath)
        if translated is None:
            raise
        raise translated from exc
