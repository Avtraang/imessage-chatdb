"""Message text search and its two pure helpers (DESIGN.md sections 4.3, 4.4, 6.5).

``search`` runs the relay's SEARCH statement verbatim: ``m.text LIKE ?`` plus
four ``instr(m.attributedBody, CAST(? AS BLOB)) > 0`` variants (the query as
typed, lower-cased, capitalised and upper-cased), tapbacks excluded with
``IFNULL(m.associated_message_type, 0) = 0``, ``ORDER BY m.ROWID DESC`` and
``LIMIT limit * oversample``.  ``LIKE`` stops at the first NUL in a typedstream
blob, which is why the blob is searched byte-wise with ``instr``; because that
also matches class names and attribute keys inside the archive, every SQL hit
is rechecked in Python against ``clean_text(effective_text(...))`` before it is
returned.

``extract_urls`` and ``snippet`` are the relay's expressions verbatim; the
relay keeps its own result-title prefix, short-query guard and contact
resolution.

``search(..., chat_guid=...)`` (0.1.1) runs ``SEARCH_IN_CHAT`` instead: the
same statement with one ``AND c.guid = ?`` line, so the oversample, recheck
and cap apply to that chat's rows only.
"""

from __future__ import annotations

import re
import sqlite3

from . import sql
from .models import SearchHit
from .typedstream import clean_text, effective_text

__all__ = ["extract_urls", "snippet", "search"]

#: The relay's URL pattern: a scheme followed by non-space.
URL_RE = re.compile(r"https?://\S+")

#: Trailing characters stripped from each URL (``u.rstrip(".,);")``).
URL_TRAILING = ".,);"

#: The ellipsis the relay puts at a clipped end of a snippet.
ELLIPSIS = "…"

#: ``chat.style`` of a group chat; repeated here so this module stays independent
#: of ``handles``.
_GROUP_STYLE = 43


def extract_urls(text: str | None) -> list[str]:
    """``re.findall(r"https?://\\S+")`` with each hit ``rstrip(".,);")``-ed.

    Order of appearance is kept and duplicates are **not** removed (the relay
    dedupes across messages itself).  ``None`` -> ``[]``.
    """
    if not text:
        return []
    return [u.rstrip(URL_TRAILING) for u in URL_RE.findall(text)]


def snippet(text: str, index: int, *, before: int = 40, width: int = 120) -> str:
    """The relay's search snippet window around a match at ``index``.

    ``start = max(0, index - before)``; the result is ``text[start:start + width]``
    with ``"…"`` prepended when ``start > 0`` and appended when the text
    continues past the window -- the relay's expression verbatim.
    """
    start = max(0, index - before)
    end = start + width
    return (ELLIPSIS if start else "") + text[start:end] + (ELLIPSIS if len(text) > end else "")


def _rows(
    conn: sqlite3.Connection, statement: str, params: tuple[object, ...]
) -> list[sqlite3.Row]:
    """Execute and ``fetchall()`` with name-addressable rows regardless of ``conn.row_factory``."""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    try:
        return list(cur.execute(statement, params).fetchall())
    finally:
        cur.close()


def search(
    conn: sqlite3.Connection,
    q: str,
    *,
    limit: int = 30,
    oversample: int = 2,
    chat_guid: str | None = None,
) -> list[SearchHit]:
    """Full-scan text search, newest first, at most ``limit`` hits.

    ``q`` is stripped; an empty query returns ``[]`` without touching the
    database.  The SQL is the relay's SEARCH statement verbatim with
    ``LIMIT limit * oversample``; each row is then rechecked with
    ``clean_text(effective_text(text, attributed_body)).lower().find(q.lower())``
    and dropped when that is negative (bytes that only occur in the archive's
    class names or attribute keys).  Because the oversample is fixed, a run of
    false positives newer than the real hits can hide them; raise ``oversample``
    or scope the query when that matters.

    ``chat_guid`` scopes the search to one chat (``chat.guid = ?``, bound as a
    literal) through ``sql.SEARCH_IN_CHAT``; the oversample, recheck and cap
    then apply to that chat's rows alone, so hits in other chats never consume
    the budget.  A guid with no matching chat yields ``[]``.  With
    ``chat_guid=None`` (the default) the global path runs exactly as before.

    ``SearchHit.text`` is the cleaned text and ``match_index`` the index the
    recheck found; ``is_from_me`` is a real ``bool``.
    """
    q = q.strip()
    if not q:
        return []
    like = f"%{q}%"
    variants = (q, q.lower(), q.capitalize(), q.upper())
    needle = q.lower()
    if chat_guid is None:
        rows = _rows(conn, sql.SEARCH, (like, *variants, limit * oversample))
    else:
        rows = _rows(conn, sql.SEARCH_IN_CHAT, (like, *variants, chat_guid, limit * oversample))
    out: list[SearchHit] = []
    for r in rows:
        text = clean_text(effective_text(r["text"], r["attributed_body"]))
        i = text.lower().find(needle)
        if i < 0:
            continue
        out.append(
            SearchHit(
                rowid=int(r["rowid"]),
                chat_rowid=int(r["chat_rowid"]),
                chat_guid=str(r["chat_guid"]),
                chat_identifier=r["chat_identifier"],
                chat_display_name=r["chat_name"],
                is_group=r["chat_style"] == _GROUP_STYLE,
                date=int(r["date"] or 0),
                is_from_me=bool(r["is_from_me"]),
                sender_handle=r["sender"],
                text=text,
                match_index=i,
            )
        )
        if len(out) >= limit:
            break
    return out
