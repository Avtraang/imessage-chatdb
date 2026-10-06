"""``python -m imessage_chatdb`` - a small JSON-lines command line (DESIGN.md section 4.7).

Subcommands::

    check                       open the database, print profile + max ROWID + "Full Disk Access OK"
    tail [--since-rowid N] [--follow] [--limit N] [--interval S]
    chats [--limit N]
    search TEXT [--limit N] [--chat GUID]

Every subcommand takes ``--db PATH`` (default ``~/Library/Messages/chat.db``).
``tail``, ``chats`` and ``search`` print one JSON object per line from
``Message.to_dict()`` / ``ChatSummary.to_dict()`` / ``SearchHit.to_dict()``;
``check`` prints one JSON object describing the database.

Privacy: ``tail``, ``chats`` and ``search`` print message content (text,
handles, chat names) to stdout as JSON lines, so do not point them at a log
file or a remote pipe; the library itself never logs or prints message
content.  ``check`` prints only the path, profile and max ROWID.

Exit codes: 0 success; 1 any other ``ChatDBError`` (busy, schema); 2 the
database could not be opened (``ChatDBAccessError``: the message, which names
the path and the interpreter binary that needs Full Disk Access, goes to
stderr); 64 (``EX_USAGE``) a command-line usage error (unknown subcommand, bad
flag or value -- argparse's default of 2 is *not* used, so a health probe that
treats 2 as "Full Disk Access lost" cannot misfire on a typo); 130 interrupted.
``--help`` exits 0.  A stdout closed early by its reader (``tail ... | head
-1``) is not an error: output stops, nothing is printed about the broken pipe
(stdout is pointed at ``os.devnull`` so the interpreter's shutdown flush cannot
fail again) and the exit code is 0.

The CLI is built on the module-level query functions rather than the
``ChatDB`` facade so it stays a thin, dependency-free layer; it opens one
connection per command (and one per polling round in ``--follow`` mode),
exactly as the facade does.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from typing import IO, Any, NoReturn

from .chats import chats_by_activity
from .connection import DEFAULT_CHATDB, open_connection
from .errors import ChatDBAccessError, ChatDBBusy, ChatDBError
from .messages import max_rowid, messages_after
from .schema import Schema
from .search import search as search_hits

__all__ = ["UsageError", "build_parser", "main"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ACCESS = 2
EXIT_USAGE = 64  # sysexits.h EX_USAGE; distinct from EXIT_ACCESS on purpose
EXIT_INTERRUPTED = 130

DEFAULT_CHATS_LIMIT = 200
DEFAULT_SEARCH_LIMIT = 30
DEFAULT_FOLLOW_INTERVAL = 2.0

#: Shown by ``--help`` (main, ``tail``, ``chats`` and ``search``): the only
#: place the library prints message content (``chats`` prints handles and
#: chat names, which count).
PRIVACY_NOTE = (
    "privacy: tail, chats and search print message content (text, handles, chat names) "
    "to stdout as JSON lines; do not point them at a log file or a remote pipe."
)


class UsageError(SystemExit):
    """A command-line usage error; ``code`` is :data:`EXIT_USAGE`.

    Raised by the parser :func:`build_parser` returns in place of argparse's
    ``SystemExit(2)`` so that exit code 2 stays reserved for "cannot open the
    database".  ``usage`` is the parser's usage line and ``message`` the
    explanation; :func:`main` writes both to its ``stderr`` stream.
    """

    def __init__(self, usage: str, message: str) -> None:
        super().__init__(EXIT_USAGE)
        self.usage = usage
        self.message = message

    def __str__(self) -> str:
        return self.message


class _Parser(argparse.ArgumentParser):
    """``ArgumentParser`` whose errors raise :class:`UsageError` (code 64, not 2).

    Subparsers are created with ``parser_class=type(self)`` by argparse, so
    every subcommand's errors take the same path.
    """

    def error(self, message: str) -> NoReturn:
        raise UsageError(self.format_usage(), f"{self.prog}: error: {message}")


def build_parser() -> argparse.ArgumentParser:
    """The argparse parser; ``--db`` is accepted after every subcommand.

    Usage errors raise :class:`UsageError` (a ``SystemExit`` with code 64)
    instead of exiting with argparse's 2; ``--help`` still exits 0.
    """
    common = _Parser(add_help=False)  # same class so subparsers accept it as a parent
    common.add_argument(
        "--db",
        default=DEFAULT_CHATDB,
        metavar="PATH",
        help=f"path to chat.db (default: {DEFAULT_CHATDB})",
    )

    parser = _Parser(
        prog="python -m imessage_chatdb",
        description="Read-only JSON-lines access to an Apple Messages chat.db.",
        epilog=PRIVACY_NOTE,
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    sub.add_parser(
        "check",
        parents=[common],
        help="open the database and report the schema profile and max ROWID",
    )

    tail = sub.add_parser(
        "tail",
        parents=[common],
        help="print messages with ROWID > N (default: start at now, like watch())",
        epilog=PRIVACY_NOTE,
    )
    tail.add_argument(
        "--since-rowid",
        type=int,
        default=None,
        metavar="N",
        help="print messages with ROWID > N; omitted = start at MAX(ROWID) (no replay)",
    )
    tail.add_argument(
        "--follow",
        action="store_true",
        help="keep polling for new messages until interrupted",
    )
    tail.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="cap the number of messages printed in one round",
    )
    tail.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_FOLLOW_INTERVAL,
        metavar="SECONDS",
        help=f"seconds between polls with --follow (default: {DEFAULT_FOLLOW_INTERVAL})",
    )

    chats = sub.add_parser(
        "chats",
        parents=[common],
        help="list chats, most recent activity first",
        epilog=PRIVACY_NOTE,
    )
    chats.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_CHATS_LIMIT,
        metavar="N",
        help=f"maximum number of chats (default: {DEFAULT_CHATS_LIMIT})",
    )

    srch = sub.add_parser(
        "search", parents=[common], help="search message text, newest first", epilog=PRIVACY_NOTE
    )
    srch.add_argument("text", metavar="TEXT", help="text to search for (case-insensitive)")
    srch.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_SEARCH_LIMIT,
        metavar="N",
        help=f"maximum number of hits (default: {DEFAULT_SEARCH_LIMIT})",
    )
    srch.add_argument(
        "--chat",
        default=None,
        metavar="GUID",
        help="only search messages of the chat with this guid (default: every chat)",
    )
    return parser


def _emit(out: IO[str], obj: dict[str, Any]) -> None:
    out.write(json.dumps(obj, ensure_ascii=False))
    out.write("\n")
    out.flush()


def _discard_further_output(out: IO[str]) -> None:
    """After a ``BrokenPipeError`` on ``out``, point its descriptor at ``os.devnull``.

    The reader of our stdout went away (``| head -1``).  Python flushes the
    standard streams again at interpreter shutdown, which would raise a second
    ``BrokenPipeError`` ("Exception ignored in ...") and turn the exit code
    into 120; redirecting the descriptor first is the recipe from the
    ``signal`` module documentation.  Only the stream that actually broke is
    touched: an injected ``stdout`` without a descriptor (a test double, an
    ``io.StringIO``) leaves the process's real stdout alone.
    """
    try:
        fd = out.fileno()
    except (AttributeError, OSError, ValueError):  # io.UnsupportedOperation is both
        return
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, fd)
    finally:
        os.close(devnull)


def _cmd_check(args: argparse.Namespace, out: IO[str]) -> int:
    conn = open_connection(args.db)
    try:
        schema = Schema(conn)
        schema.check()
        top = max_rowid(conn)
    finally:
        conn.close()
    _emit(
        out,
        {
            "ok": True,
            "path": args.db,
            "profile": schema.profile,
            "max_rowid": top,
            "optional_missing": sorted(schema.optional_missing),
            "message": "Full Disk Access OK",
        },
    )
    return EXIT_OK


def _tail_round(
    db_path: str, since: int | None, limit: int | None, out: IO[str], err: IO[str]
) -> int:
    """One ``messages_after`` round on a fresh connection; returns the new cursor."""
    conn = open_connection(db_path)
    try:
        schema = Schema(conn)
        top = max_rowid(conn)
        cursor = top if since is None else since
        if cursor > top:
            # The database was rebuilt (CursorAhead in watch terms); restart at now.
            err.write(f"warning: rowid {cursor} is ahead of MAX(ROWID) {top}; restarting at now\n")
            cursor = top
        for m in messages_after(conn, schema, cursor, limit=limit):
            _emit(out, m.to_dict())
            cursor = m.rowid
        return cursor
    finally:
        conn.close()


def _cmd_tail(
    args: argparse.Namespace, out: IO[str], err: IO[str], sleep: Callable[[float], None]
) -> int:
    since: int | None = args.since_rowid
    if not args.follow:
        _tail_round(args.db, since, args.limit, out, err)
        return EXIT_OK
    cursor = since
    try:
        while True:
            try:
                cursor = _tail_round(args.db, cursor, args.limit, out, err)
            except ChatDBBusy:
                pass  # retry next round, nothing advanced
            sleep(args.interval)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED


def _cmd_chats(args: argparse.Namespace, out: IO[str]) -> int:
    conn = open_connection(args.db)
    try:
        for c in chats_by_activity(conn, limit=args.limit):
            _emit(out, c.to_dict())
    finally:
        conn.close()
    return EXIT_OK


def _cmd_search(args: argparse.Namespace, out: IO[str]) -> int:
    conn = open_connection(args.db)
    try:
        for hit in search_hits(conn, args.text, limit=args.limit, chat_guid=args.chat):
            _emit(out, hit.to_dict())
    finally:
        conn.close()
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run the CLI and return its exit code; it never raises ``SystemExit``.

    ``stdout``/``stderr`` default to ``sys.stdout``/``sys.stderr``; ``sleep`` is
    the pause between ``--follow`` rounds (injectable for tests).  A usage error
    returns :data:`EXIT_USAGE` (64) with the usage line and explanation on
    ``stderr``; ``--help`` prints to ``sys.stdout`` and returns 0.  A
    ``BrokenPipeError`` from ``stdout`` (its reader closed the pipe) ends the
    command with :data:`EXIT_OK` after redirecting the broken descriptor to
    ``os.devnull``, so nothing is printed at shutdown.
    """
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    try:
        args = build_parser().parse_args(argv)
    except UsageError as exc:
        err.write(exc.usage)
        err.write(f"{exc.message}\n")
        return EXIT_USAGE
    except SystemExit as exc:  # --help; argparse exits 0 after printing
        return EXIT_OK if exc.code in (None, 0) else EXIT_USAGE
    try:
        if args.command == "check":
            return _cmd_check(args, out)
        if args.command == "tail":
            return _cmd_tail(args, out, err, sleep)
        if args.command == "chats":
            return _cmd_chats(args, out)
        if args.command == "search":
            return _cmd_search(args, out)
        raise AssertionError(f"unhandled command {args.command!r}")  # pragma: no cover
    except ChatDBAccessError as exc:
        err.write(f"error: {exc}\n")
        return EXIT_ACCESS
    except ChatDBError as exc:
        err.write(f"error: {exc}\n")
        return EXIT_ERROR
    except sqlite3.Error as exc:
        err.write(f"error: sqlite3: {exc}\n")
        return EXIT_ERROR
    except BrokenPipeError:
        _discard_further_output(out)
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
