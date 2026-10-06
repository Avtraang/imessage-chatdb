# imessage-chatdb — design

> Written before the first line of code, from three independent proposals and two judge reviews. `<relay>` is the private relay this library was extracted from; its line numbers refer to that relay's source at extraction time.

Package `imessage-chatdb`, import `imessage_chatdb`. Future home `<this repo>` (not created by this document). Python >= 3.12, standard library only at runtime, MIT.

This document is the single design the implementation follows. It is built from three proposals and two judge verdicts.

## 0. How the verdicts were resolved

Rank sums tie (P1 = 1+3, P2 = 3+1, P3 = 2+2). P3 is second for both judges; P1 and P2 each won once. The tie is broken by the task's one hard, measurable constraint — the relay's HTTP JSON must not change — on which the judges agree P2 scores highest (9/10 twice) and from which both judges grafted the same rules. So:

- **Structural base = P2 (relay-first).** Module functions take an open `sqlite3.Connection` (plus a `Schema` where the SELECT is needed), SQL text is kept verbatim wherever row order or row set is JSON-visible, `apple_to_unix` is the relay's exact expression, the relay keeps a `db()` name returning a fresh per-call connection, the attachment filter is `include_plugin_payloads`, and the cutover is phased (core -> endpoints -> `fetch_threads`).
- **Correctness layer = P3.** `effective_text` as the only exact statement of the relay's text rule; `extract_text` with the `n >= 0x80 -> None` guard and 4/8-byte `0x82`/`0x83` reads; connection invariants (`mode=ro` URI + `PRAGMA query_only`, autocommit, every query `fetchall()`-ed, never `immutable=1`); `ChatDBBusy` -> `([], cursor)` and `ChatDBAccessError` naming `sys.executable`; `CursorAhead`; `include_orphans`; raw Apple-int dates on the dataclasses; four schema profiles; WAL-vs-immutable and `BEGIN EXCLUSIVE` tests; fuzz; counts-only shadow diff plus an HTTP-side diff; `Message.to_dict()` with relay keys and raw values.
- **Ergonomic surface = P1.** `imessage_chatdb.open()`, `ChatDB.watch()` generator with a frozen `Cursor`, `poll()` as the primitive, `FullDiskAccessRequired`-style error text, the `python -m imessage_chatdb` CLI, the `CASE WHEN balloon_bundle_id ... THEN payload_data END` trim (output-neutral: `row_to_msg` only parses the payload for the URL balloon), and a **committed** `tests/test_relay_compat.py` in the relay repo.
- **Rule carried from P2, applied everywhere:** *facts live in the library, strings live in the relay.* `TAPBACK_VERBS`, "Photo"/"Video"/"Attachment" previews, "You", "me", first-name formatting, `group_title`, `att_public` URLs, HEIC advertising, `/link_image/{rowid}` paths are relay code. P1's `one_line_summary`, `Tapback.verb`, `format_group_title` are dropped.
- **Scope cut from P3:** the full `TypedStreamReader` + `MessagePart` parts API, `edit_history()`, and retraction events are **not** in v0.1 (judge 1 objection 12). v0.1 ships the byte-scan `extract_text` only; the reader lands in v0.2 as a separate module with the fast path as its oracle. *(Shipped in 0.2.0 — §14.)*

Every objection from both judges is resolved in §13 with a one-line disposition. Facts stated here were verified read-only against the live macOS 27 database by the proposals and the judges (column names and counts only; no content was read into any document).

---

## 1. Goals and non-goals

### Goals
1. A stranger on a Mac can `pip install imessage-chatdb` and, in 40 lines, open `chat.db` read-only, tail new messages since a ROWID cursor, detect edits by `date_edited`, decode `attributedBody`, classify tapbacks (2000-2006 add / 3000-3006 remove / 1000 sticker, with macOS 26+ `associated_message_emoji`), follow replies (`thread_originator_guid`), read link previews (`payload_data`), list attachments, list chats and participants, classify service (iMessage / SMS / RCS / iMessageLite), convert Apple epochs (0 = never), tell group (43) from 1:1 (45), match a chat by participants, search text and blob content NUL-safely, and keep working when a column is missing on an older macOS.
2. The relay (`<relay>/relay.py`) imports the library and its HTTP JSON stays **byte-identical** (same keys, same order, same floats, same `null`/`""`/`true` values).
3. Tests never touch a real database.

### Non-goals (v0.1)
- Contact resolution, display names, presentation strings (relay / caller).
- Attachment URLs, HEIC/JPEG transcoding, thumbnails (relay).
- Relay state: reads, pins, archive, auto-translate, icons, forced-unread (relay).
- Writing to chat.db, sending messages, AppleScript/Shortcuts (never).
- Edit history from `message_summary_info`, FTS (later). *(The full typedstream parts API and retraction events were v0.2+ here and shipped in 0.2.0 — §14.)*
- Any dependency beyond the standard library at runtime.

---

## 2. Verified facts the design rests on (macOS 27.0, SQLite WAL)

- `attributedBody` begins `04 0B "streamtyped" 81 E8 03`. The text is one `+`-typed value: `84 01 2B <len> <utf-8>`. Length tags seen: one-byte (`< 0x80`) and `0x81`+u16le; zero `0x82`/`0x83` (the largest text is under 10 KB, the largest blob under 50 KB). The byte after the string is `0x86` then `84 02 69 49` on every blob. Two root classes exist: plain `NSAttributedString` (most blobs) and `NSMutableAttributedString` (the rest, roughly a third). The relay reads **3** bytes after `0x82`; the spec and reference parsers read **4**. Latent bug, no live impact; the library reads 4 (and 8 for `0x83`).
- `message`: name-based reads only (macOS 27 reordered the table and dropped all `sr_*`). `date_*` are INTEGER ns since 2001-01-01; `date_edited` is `0` (not NULL) on unedited rows. `text IS NULL` with a blob on most rows (tens of thousands). A few hundred rows have no `chat_message_join` row (the relay's inner join hides them). No message is joined to more than one chat (none).
- `associated_message_type` seen: 0, 1000, 2000-2006, 3000-3002. Every 2006 row has `associated_message_emoji`. `associated_message_guid` forms: `p:<part>/<GUID>` (dominant), `bp:<GUID>` (a few dozen rows, some of them emoji tapbacks), bare `<GUID>` (a handful). `thread_originator_part` is TEXT.
- `chat.style` ∈ {43 group, 45 one-to-one}; every `chat.guid` starts with `any;` so the relay's `guid.startswith("iMessage")` tie-break is a no-op here (kept verbatim for fidelity).
- `message.service` ∈ {iMessage, SMS, RCS, iMessageLite}; `chat.service_name` ∈ {iMessage, SMS, RCS} and is a stale hint.
- `attachment.filename`: `~/`-prefixed (the vast majority, thousands), absolute `/var/...` (a few hundred), NULL (a few hundred). Roughly a third of the rows (thousands) are `*.pluginPayloadAttachment`. `hide_attachment=1` = those **plus a couple of dozen image/png rows that are not plugin payloads and are surfaced by the relay today**; all stickers (a few dozen) have `hide_attachment=0`. Therefore the default exclusion is the `transfer_name` suffix, never `hide_attachment`.
- `immutable=1` ignores the WAL and misses fresh rows (a note in the original relay's own documentation). `mode=ro` honours the WAL and needs the `-shm` file, which Messages.app keeps present.
- Full Disk Access is bound to the exact interpreter binary (`brew upgrade python` orphans it).

---

## 3. Package layout

```
imessage-chatdb/
  pyproject.toml              hatchling; name imessage-chatdb; requires-python >=3.12; dependencies = []
  README.md  LICENSE (MIT)  CHANGELOG.md
  docs/TYPEDSTREAM.md         0.2: the attributedBody grammar, verified facts, fixture writer and reader API (§14)
  src/imessage_chatdb/
    __init__.py               open(), ChatDB, models, pure functions, errors, __version__
    py.typed
    errors.py                 ChatDBError, ChatDBAccessError, ChatDBBusy, CursorAhead, SchemaError
    dates.py                  APPLE_EPOCH_OFFSET, apple_to_unix, unix_to_apple, apple_to_datetime
    typedstream.py            extract_text, effective_text, clean_text     (byte-scan fast path only in v0.1)
    typedstream_reader.py     0.2 (§14): TypedStreamError, AttributedBody, AttributeRun, Url, UnknownValue, parse_attributed_body,
                              try_parse_attributed_body, Mention, TextPart, AttachmentPart, UnknownPart, Part, message_parts
    keyed_archive.py          KeyedArchive: plistlib + UID deref (visited set), string()/nsurl() helpers
    link_preview.py           LINK_BALLOON, SKIP_IMG, LinkPreview, parse_link_preview, embedded_image, sniff_image_mime
    reactions.py              Reaction, classify_reaction, parse_associated_guid
    services.py               Service(StrEnum), normalize_service, service_family
    handles.py                address_key, is_email, is_group_style
    connection.py             DEFAULT_CHATDB, open_connection
    schema.py                 Schema (PRAGMA table_info introspection), REQUIRED/OPTIONAL columns, build_message_select
    sql.py                    every statement whose output order is JSON-visible, verbatim from the relay
    models.py                 frozen slotted dataclasses (§5)
    messages.py               row_to_message, messages_after, messages_edited_after, max_rowid, max_date_edited,
                              thread_messages, recent_messages, message, messages_by_guid, reply_targets, enrich
    attachments.py            is_plugin_payload, attachments_for, attachment_by_guid, chat_attachments, payload_for
    chats.py                  chats_by_activity, chat, chat_by_rowid, participants, participants_map, chat_services,
                              last_rowid_for, chats_changed_since, unread_count, lite_messages, one_to_one_activity, find_chat
    search.py                 search, extract_urls, snippet
    polling.py                Cursor, Event, poll_once, watch, run_watch (all re-exported; `imessage_chatdb.watch` is the generator)
    db.py                     ChatDB facade (one connection per call; connection() for batching)
    __main__.py               python -m imessage_chatdb check|tail|chats|search   (0.2: tail/search --parts)
  tests/
    conftest.py               refuses any path under ~/Library; builds tmp_path WAL databases per profile
    fixtures/schema_profiles.py   macos14 / macos15 / macos26 / macos27 DDL subsets with real key constraints
    fixtures/builders.py          add_handle / add_chat / add_message / add_attachment
    fixtures/typedstream_writer.py   encode_attributed_body(text, *, mutable=False, force_len_tag=None)
    fixtures/keyed_archive_writer.py make_link_payload(...)
                              (0.2: typedstream_writer.py also holds encode_attributed_body_runs, the byte-exact archiver)
    test_dates.py test_typedstream.py test_link_preview.py test_reactions.py test_services.py
    test_typedstream_reader.py test_retractions.py                        (0.2, §14)
    test_schema.py test_connection.py test_messages.py test_edits.py test_attachments.py
    test_chats.py test_find_chat.py test_search.py test_watch.py test_cli.py test_fuzz.py
```

Dependency direction: `dates`, `services`, `handles`, `reactions`, `typedstream`, `keyed_archive` <- `link_preview` are leaves; `models` imports leaves; `schema`/`sql` are leaves; `messages`/`attachments`/`chats`/`search` import `models`+`sql`+`schema`; `polling` imports `messages`; `db` imports everything. Nothing imports FastAPI, httpx, or anything outside the stdlib.

---

## 4. Public API (full signatures)

### 4.1 Opening

```python
DEFAULT_CHATDB = "~/Library/Messages/chat.db"

def open_connection(path: str | os.PathLike = DEFAULT_CHATDB, *, timeout: float = 5.0,
                    readonly_uri: bool = True) -> sqlite3.Connection:
    """readonly_uri=True (default, the only documented mode):
         sqlite3.connect(f"file:{quote(abspath)}?mode=ro", uri=True, timeout=timeout, isolation_level=None)
         PRAGMA query_only = 1; SELECT 1 FROM sqlite_master LIMIT 1 (forces the open so FDA errors surface here)
         row_factory = sqlite3.Row
       readonly_uri=False: plain sqlite3.connect(abspath, timeout, isolation_level=None) + PRAGMA query_only = 1.
         Compatibility shim for the relay's historical connect. Documented as such, not as a feature;
         SQLite may create -wal/-shm. Never immutable=1 in either mode.
       Raises ChatDBAccessError ("unable to open database file" / "authorization denied" / "not a database"),
       with a message naming sys.executable and System Settings > Privacy & Security > Full Disk Access.
       Raises ChatDBBusy on "locked"/"busy". Other OperationalErrors propagate."""

def open(path=DEFAULT_CHATDB, *, timeout=5.0) -> "ChatDB"          # module-level convenience

class ChatDB:
    def __init__(self, path=DEFAULT_CHATDB, *, timeout: float = 5.0, readonly_uri: bool = True)
    path: Path
    schema: Schema                                   # introspected once, lazily; refresh_schema()
    def connect(self) -> sqlite3.Connection           # a NEW connection each call; caller closes
    def connection(self) -> ContextManager[sqlite3.Connection]   # batch several calls on one connection
    def refresh_schema(self) -> Schema
    def close(self) -> None                           # no-op placeholder (nothing is held)
```

`ChatDB` opens **one connection per public call** (what the relay does today; ~0.3 ms) and closes it in `finally`. Connections are never shared across threads; `check_same_thread` stays default. `asyncio.to_thread(db.poll, cursor)` is the async pattern. Every query ends in `fetchall()`; no generator exposes a live SQLite cursor, so no read snapshot lingers in WAL.

### 4.2 Schema

```python
class Schema:
    def __init__(self, conn: sqlite3.Connection)      # PRAGMA table_info on message, chat, handle, attachment,
                                                      # chat_message_join, chat_handle_join, message_attachment_join
    columns: dict[str, dict[str, str]]                # table -> {column: decltype}
    def has(self, table: str, column: str) -> bool
    optional_missing: frozenset[str]                  # "message.date_edited", ...
    profile: str                                      # "macos14" | "macos15" | "macos26" | "macos27" | "unknown" (best effort)
    def check(self) -> None                           # raises SchemaError("message.guid") for the first missing required column

REQUIRED = {
  "message": ["ROWID","guid","text","attributedBody","date","is_from_me","handle_id",
              "cache_has_attachments","associated_message_guid","associated_message_type"],
  "chat": ["ROWID","guid","style","chat_identifier","display_name"],
  "handle": ["ROWID","id"],
  "attachment": ["ROWID","guid","filename","mime_type","transfer_name"],
  "chat_message_join": ["chat_id","message_id"], "chat_handle_join": ["chat_id","handle_id"],
  "message_attachment_join": ["message_id","attachment_id"],
}
OPTIONAL = {
  "message": ["date_read","date_delivered","date_edited","date_retracted","date_updated","service",
              "associated_message_emoji","balloon_bundle_id","payload_data","thread_originator_guid",
              "thread_originator_part","item_type","group_action_type","group_title","filter_action","is_spam",
              "expressive_send_style_id"],
  "chat": ["service_name","is_archived","is_filtered","group_id","last_read_message_timestamp"],
  "handle": ["service","country","uncanonicalized_id","person_centric_id"],
  "attachment": ["uti","total_bytes","is_sticker","hide_attachment","created_date"],
}
def build_message_select(schema: Schema, *, include_orphans: bool = False) -> str
```

`service`, `balloon_bundle_id` and `payload_data` are **optional** (judge 2 objection 8). Missing optional columns render as `NULL AS alias`; the model field is `None`; `messages_edited_after` returns `([], mark)` when `date_edited` is absent; `chat_services` falls back to `chat.service_name` only when `message.service` is absent.

### 4.3 Pure functions (module level, re-exported from `__init__`)

```python
# dates.py
APPLE_EPOCH_OFFSET = 978307200
def apple_to_unix(raw: int | float | None) -> float | None:
    if not raw: return None                 # 0 and None are "never" (date_edited, date_read, date_retracted)
    v = float(raw)
    if v > 1e11: v = v / 1e9               # ns (10.13+) vs seconds (older)
    return v + APPLE_EPOCH_OFFSET          # the relay's expression verbatim -> bit-identical floats
def unix_to_apple(ts: float) -> int         # int(round((ts - OFFSET) * 1e9))
def apple_to_datetime(raw, tz: tzinfo | None = timezone.utc) -> datetime | None

# typedstream.py
def extract_text(data: bytes | None) -> str | None:
    """Locate b'NSString', skip 6, find 0x2b, read the length tag:
       < 0x80 one byte | 0x81 + u16le | 0x82 + u32le | 0x83 + u64le | any other >= 0x80 -> None.
       utf-8 errors='replace'; '' -> None; any exception -> None. Never raises."""
def effective_text(text_column: str | None, blob: bytes | None) -> str | None:
    """The relay's row_to_msg rule exactly: text_column if truthy; else extract_text(blob) if blob;
       else text_column unchanged ('' stays '', None stays None)."""
def clean_text(s: str | None) -> str        # remove U+FFFC, collapse whitespace; '' for None

# keyed_archive.py
class KeyedArchive:
    @classmethod def load(cls, payload: bytes) -> "KeyedArchive"     # plistlib.loads; validates $objects/$top; raises ValueError
    objects: list; root: object
    def deref(self, v, *, max_depth: int = 32) -> object              # UID chain with a visited set; None when out of range
    def string(self, obj: dict, *keys: str) -> str | None             # first non-empty str; unwraps archived NSURL (NS.relative)
    def nested_url(self, obj: dict, key: str) -> str | None           # obj[key] -> dict -> URL (NSURL unwrapped), must start with http

# link_preview.py
LINK_BALLOON = "com.apple.messages.URLBalloonProvider"
SKIP_IMG = ("favicon", "apple-touch", ".ico")
@dataclass(frozen=True, slots=True)
class LinkPreview: url: str | None; title: str | None; summary: str | None; site: str | None
                   image_url: str | None; has_embedded_image: bool
def parse_link_preview(payload: bytes | None) -> LinkPreview | None   # §6.3; never raises
def embedded_images(payload: bytes | None) -> list[bytes] | None     # None = empty/not a plist/no $objects list; [] = parsed, no blob; else blobs in table order
def embedded_image(payload: bytes | None) -> bytes | None             # max(embedded_images(payload), key=len): largest PNG/JPEG/ftyp blob > 3000 bytes
def sniff_image_mime(head: bytes) -> str                              # png/jpeg/heic/gif/webp -> mime, else application/octet-stream

# reactions.py
@dataclass(frozen=True, slots=True)
class Reaction: action: Literal["add","remove"]; kind: str; emoji: str | None; raw_type: int
                target_guid: str | None; target_part: int | None; is_balloon: bool
    # kind in {"love","like","dislike","laugh","emphasize","question","emoji","sticker","unknown"}
def classify_reaction(assoc_type: int | None, assoc_guid: str | None, emoji: str | None = None) -> Reaction | None
def parse_associated_guid(s: str | None) -> tuple[str | None, int | None, bool]   # (target_guid, part, is_balloon)
    # "p:3/G" -> ("G", 3, False); "bp:G" -> ("G", None, True); "G" -> ("G", None, False); None -> (None, None, False)

# services.py
class Service(StrEnum): IMESSAGE="iMessage"; IMESSAGE_LITE="iMessageLite"; SMS="SMS"; RCS="RCS"; UNKNOWN="unknown"
def normalize_service(s: str | None) -> Service          # strip, case-insensitive; unknown -> UNKNOWN; never raises
def service_family(s: str | None) -> str | None          # the relay's _norm_service verbatim:
                                                          # startswith("iMessage") -> "iMessage"; upper in (RCS, SMS) -> that; else None

# handles.py
def address_key(addr: str | None) -> str | None          # None/'' -> None; '@' -> strip().lower(); else digits, last 10 when >= 10
def is_email(addr: str) -> bool
def is_group_style(style: int | None) -> bool            # style == 43

# search.py
def extract_urls(text: str | None) -> list[str]          # re.findall(r"https?://\S+"), each rstrip(".,);"), order kept, no dedupe
def snippet(text: str, index: int, *, before: int = 40, width: int = 120) -> str   # relay window with … markers
```

### 4.4 Query functions (take `conn`, and `schema` where the message SELECT is used)

```python
# messages.py
def row_to_message(r: sqlite3.Row) -> Message
def messages_after(conn, schema, rowid: int, *, limit: int | None = None, enrich: bool = True,
                   include_orphans: bool = False) -> list[Message]            # WHERE m.ROWID > ? ORDER BY m.ROWID ASC
def messages_edited_after(conn, schema, mark: int, *, enrich: bool = True) -> tuple[list[Message], int]
                   # WHERE m.date_edited > ? ORDER BY m.date_edited ASC; new mark = max raw date_edited seen, never regresses
                   # ([], mark) when message.date_edited is absent
def max_rowid(conn) -> int                                                   # MAX(ROWID) or 0
def max_date_edited(conn, schema) -> int                                     # MAX(date_edited) or 0; 0 when column absent
def thread_messages(conn, schema, chat_guid: str, *, limit: int = 50, before_rowid: int | None = None,
                    enrich: bool = True) -> list[Message]                    # ROWID DESC LIMIT, then reversed (ascending)
def recent_messages(conn, schema, chat_guid: str, *, limit: int = 1000, enrich: bool = False) -> list[Message]
                   # ROWID DESC LIMIT, NOT reversed (newest first) — thread_media's link scan order
def message(conn, schema, rowid: int, *, enrich: bool = True) -> Message | None
def messages_by_guid(conn, schema, guids: Iterable[str], *, enrich: bool = True) -> dict[str, Message]
def reply_targets(conn, guids: Iterable[str]) -> dict[str, ReplyTarget]    # one IN query; text = clean_text(effective_text(...)) untruncated, '' if none
def enrich(conn, msgs: list[Message]) -> list[Message]                      # attachments only for has_attachments rows (one IN query), reply targets (one IN query)

# attachments.py
def is_plugin_payload(transfer_name: str | None) -> bool                    # (transfer_name or "").endswith(".pluginPayloadAttachment")
def attachments_for(conn, rowids: Iterable[int], *, include_plugin_payloads: bool = False) -> dict[int, list[Attachment]]
def attachment_by_guid(conn, guid: str) -> Attachment | None
def chat_attachments(conn, chat_guid: str, *, include_plugin_payloads: bool = False) -> list[Attachment]   # ORDER BY m.date DESC, verbatim
def payload_for(conn, rowid: int) -> bytes | None                            # SELECT payload_data FROM message WHERE ROWID = ?

# chats.py
def chats_by_activity(conn, *, limit: int = 200) -> list[ChatSummary]       # relay's fetch_threads head query verbatim
def chat(conn, guid: str) -> Chat | None
def chat_by_rowid(conn, rowid: int) -> Chat | None
def participants(conn, chat_rowid: int) -> list[str]                        # relay's group_title SELECT verbatim (no ORDER BY)
def participants_map(conn) -> dict[int, list[str]]                          # relay's fetch_threads parts SELECT verbatim
def chat_services(conn, chat_rowids: Iterable[int]) -> dict[int, str | None]
                   # relay's two-correlated-subquery statement verbatim; value = service_family(out) or service_family(any) or service_family(chat)
                   # RAISES on SQL failure (the relay wrapper keeps its try/except -> {})
def last_rowid_for(conn, chat_rowid: int) -> int
def chats_changed_since(conn, rowid: int) -> list[ChatActivity]              # 0.2 library addition: chat_message_join only,
                   # SELECT chat_id, MAX(message_id) WHERE message_id > ? GROUP BY chat_id ORDER BY last DESC (one index range scan)
def unread_count(conn, chat_rowid: int, after_rowid: int, *, incoming_only: bool = True) -> int   # relay's UNREAD SQL verbatim
def lite_messages(conn, rowids: Iterable[int]) -> dict[int, LiteMessage]
                   # relay's last_message_previews SELECT verbatim + attachments_for(has_attachments rows) — facts only
def one_to_one_activity(conn) -> list[tuple[str, int]]                      # (chat_identifier, last_rowid) for style 45; contact_recency's SQL
def find_chat(conn, addresses: Sequence[str], *, key: Callable[[str], str | None] = address_key,
              exclude: Iterable[str] = ()) -> ChatMatch | None
                   # self = {key(a) for a in exclude}; want = {key(a) for a in addresses} - self; empty -> None
                   # 1:1 branch iff len(addresses) == 1 (the relay's rule, NOT the size of want):
                   #   style=45 rows, key(chat_identifier) in want, max by (last_rowid_for, guid.startswith("iMessage"))
                   # group branch: chat_handle_join LEFT JOIN UNION DISTINCT incoming senders, keyed, minus self,
                   #   exact set equality with want, max by last_rowid_for

# search.py
def search(conn, q: str, *, limit: int = 30, oversample: int = 2) -> list[SearchHit]
                   # q.strip(); '' -> []. Relay's SEARCH SQL verbatim (LIKE + 4 instr() variants, IFNULL(assoc,0)=0,
                   # ROWID DESC LIMIT limit*oversample); Python recheck on clean_text(effective_text()).lower().find(q.lower()); cap at limit
```

### 4.5 `ChatDB` facade (one connection per call; same names, no `conn`/`schema` arguments)

```python
class ChatDB:
    def max_rowid(self) -> int
    def max_date_edited(self) -> int
    def initial_cursor(self) -> Cursor                           # Cursor(max_rowid(), max_date_edited())
    def messages_after(self, rowid, *, limit=None, enrich=True, include_orphans=False) -> list[Message]
    def messages_edited_after(self, mark, *, enrich=True) -> tuple[list[Message], int]
    def thread_messages(self, chat_guid, *, limit=50, before_rowid=None, enrich=True) -> list[Message]
    def recent_messages(self, chat_guid, *, limit=1000, enrich=False) -> list[Message]
    def message(self, rowid, *, enrich=True) -> Message | None
    def messages_by_guid(self, guids, *, enrich=True) -> dict[str, Message]
    def reply_targets(self, guids) -> dict[str, ReplyTarget]
    def attachments_for(self, rowids, *, include_plugin_payloads=False) -> dict[int, list[Attachment]]
    def attachment(self, guid) -> Attachment | None
    def chat_attachments(self, chat_guid, *, include_plugin_payloads=False) -> list[Attachment]
    def payload(self, rowid) -> bytes | None
    def link_image(self, rowid) -> bytes | None                  # embedded_image(payload(rowid))
    def chats(self, *, limit=200) -> list[ChatSummary]           # chats_by_activity
    def chat(self, guid) -> Chat | None
    def chat_by_rowid(self, rowid) -> Chat | None
    def participants(self, chat_rowid) -> list[str]
    def participants_map(self) -> dict[int, list[str]]
    def chat_services(self, chat_rowids) -> dict[int, str | None]
    def last_rowid_for(self, chat_rowid) -> int
    def chats_changed_since(self, rowid) -> list[ChatActivity]   # 0.2
    def unread_count(self, chat_rowid, after_rowid, *, incoming_only=True) -> int
    def lite_messages(self, rowids) -> dict[int, LiteMessage]
    def one_to_one_activity(self) -> list[tuple[str, int]]
    def find_chat(self, addresses, *, key=address_key, exclude=()) -> ChatMatch | None
    def search(self, q, *, limit=30) -> list[SearchHit]
    def poll(self, cursor: Cursor, *, include_edits=True, include_retractions=False) -> tuple[list[Event], Cursor]   # 0.2 keyword
    def watch(self, cursor: Cursor | None = None, *, interval=2.0, stop: threading.Event | None = None,
              include_edits=True, include_retractions=False, sleep=time.sleep) -> Iterator[Event]        # 0.2 keyword
```

### 4.6 Watching

```python
@dataclass(frozen=True, slots=True)
class Cursor:
    rowid: int = 0           # last ROWID delivered
    edit_mark: int = 0       # raw Apple ns date_edited high-water mark (int, never converted)
    retract_mark: int = 0    # 0.2: raw date_retracted high-water mark; to_json writes it, from_json reads a missing key as 0
    def to_json(self) -> dict; @classmethod def from_json(cls, d) -> "Cursor"

@dataclass(frozen=True, slots=True)
class Event: kind: Literal["new", "edited", "retracted"]; message: Message; cursor: Cursor   # "retracted" is 0.2, opt-in
    # cursor = what to persist once THIS event is handled: "new" -> Cursor(own rowid, round's starting mark);
    # "edited" -> Cursor(round's final rowid, max(previous, raw date_edited)) -- but a row tied on date_edited with a
    #   later row in the round keeps the previous mark (only the tie's last event passes the value; the resume query is
    #   strict, so a checkpoint inside a tie replays the tie rather than skipping it); last event's cursor == poll_once's

def poll_once(db: ChatDB, cursor: Cursor, *, include_edits: bool = True, include_retractions: bool = False) -> tuple[list[Event], Cursor]:
    """One round. Rules:
       - top = max_rowid(); if top < cursor.rowid: raise CursorAhead(cursor.rowid, top)   (rebuilt DB; caller decides)
       - new = messages_after(cursor.rowid); rowid advances only to new[-1].rowid (at-least-once)
       - edits only when include_edits and schema has date_edited; mark = max raw value returned, never regresses
       - ChatDBBusy anywhere -> ([], cursor) (no events, nothing advanced)
       - 'new' events precede 'edited' events in a round
       - 0.2: unsends only when include_retractions (default False, so the 0.1 stream is unchanged) and schema has
         date_retracted; 'retracted' events follow the 'edited' ones with the same tie and cursor rules (§14)"""
def watch(db, cursor=None, *, interval=2.0, stop=None, include_edits=True, include_retractions=False, sleep=time.sleep) -> Iterator[Event]
    # cursor=None -> db.initial_cursor() (start at now, no replay); yields events; sleeps `interval` between rounds;
    # exits when stop.is_set(); CursorAhead propagates (documented)
def run_watch(db, on_event: Callable[[Event], None], *, on_cursor: Callable[[Cursor], None] | None = None, **kw) -> None
    # on_cursor fires after each advanced round, for persistence
```

Retractions (`date_retracted`) are deliberately **not** an event kind in v0.1; `Message.date_retracted` is exposed for callers to inspect (judge 1 objection 6, judge 2 objection 11: no folding of two columns into one mark). *0.2.0 adds them as the opt-in third kind with their own mark — still no folding — see §14.*

### 4.7 CLI

`python -m imessage_chatdb check` (opens the DB, prints profile + max ROWID + "Full Disk Access OK", or the access error), `tail [--since-rowid N] [--follow]`, `chats [--limit N]`, `search TEXT [--limit N]`. Output is one JSON object per line from `Message.to_dict()` / `ChatSummary.to_dict()` / `SearchHit.to_dict()`. argparse only. *0.2: `search TEXT --chat GUID` (0.1.1) and `tail --parts` / `search --parts`, which append a `"parts"` key; without the flag every line is byte-identical to 0.1.1 (§14).*

---

## 5. Data model (`models.py`)

All `@dataclass(frozen=True, slots=True)`. **Dates are raw Apple integers** (0 = never) with `*_unix` / `*_datetime` properties; `apple_to_unix` is applied at the edge (the relay adapter). Handles are raw `handle.id` strings. Booleans are real `bool` (`row_to_message` applies `bool()` to `is_from_me` and `has_attachments` — judge objections 1-13 / 2-6).

```python
class Message:
    rowid: int; guid: str
    text: str | None                      # effective_text(text_column, attributed_body)
    text_column: str | None               # raw m.text
    date: int; date_read: int | None; date_delivered: int | None; date_edited: int | None; date_retracted: int | None
    is_from_me: bool; handle_rowid: int | None; sender_handle: str | None
    chat_rowid: int | None; chat_guid: str | None   # None only for include_orphans rows (§4.4)
    chat_identifier: str | None; chat_display_name: str | None; chat_style: int | None
    service_raw: str | None
    has_attachments: bool; attachments: tuple[Attachment, ...]
    associated_guid: str | None; associated_type: int | None; associated_emoji: str | None
    reply_to_guid: str | None; reply_to_part: str | None; reply_to: ReplyTarget | None
    balloon_bundle_id: str | None; link: LinkPreview | None     # parsed only when balloon_bundle_id == LINK_BALLOON
    item_type: int | None; group_action_type: int | None; group_title: str | None
    filter_action: int | None; is_spam: bool | None; expressive_send_style_id: str | None
    enriched: bool
    attributed_body_raw: bytes | None = None   # 0.2, keyword-only: the raw blob; row_to_message fills it; not in to_dict()
    # properties
    is_group -> chat_style == 43; service -> Service; reaction -> Reaction | None; is_tapback -> bool
    body -> AttributedBody | None; parts -> tuple[Part, ...]   # 0.2: try_parse_attributed_body / message_parts, cached, never raise
    date_unix / date_read_unix / date_edited_unix / date_retracted_unix -> float | None
    datetime -> datetime | None; clean_text -> str
    def to_dict(self) -> dict   # relay keys in relay order, raw values (§5.1)

class Attachment:
    message_rowid: int; rowid: int; guid: str; mime_type: str | None; transfer_name: str | None; filename: str | None
    uti: str | None; total_bytes: int | None; is_sticker: bool | None; hide_attachment: bool | None; message_date: int | None
    is_plugin_payload -> bool (transfer_name suffix); path -> Path | None (expanduser; None when filename is NULL/''); exists() -> bool

class ReplyTarget: guid: str; text: str; is_from_me: bool; sender_handle: str | None     # text cleaned, untruncated, '' if none
class LiteMessage: rowid: int; text: str; is_from_me: bool; associated_type: int; associated_emoji: str | None
                   has_attachments: bool; sender_handle: str | None; attachments: tuple[Attachment, ...]   # text cleaned, '' if none
class Chat: rowid: int; guid: str; style: int | None; chat_identifier: str | None; display_name: str | None
            service_name: str | None; is_archived: bool | None; is_filtered: bool | None; group_id: str | None
            is_group -> bool
class ChatSummary(Chat): last_date: int | None; last_rowid: int
class ChatMatch: chat_rowid: int; chat_guid: str; chat_identifier: str | None; display_name: str | None; is_group: bool; last_rowid: int
class ChatActivity: chat_rowid: int; last_rowid: int                       # 0.2: chats_changed_since
class SearchHit: rowid: int; chat_rowid: int; chat_guid: str; chat_identifier: str | None; chat_display_name: str | None
                 is_group: bool; date: int; is_from_me: bool; sender_handle: str | None; text: str; match_index: int
```

### 5.1 `Message.to_dict()` (library-native, for adopters and the CLI; **not** what the relay serves)

Same keys and order as the relay's message dict, raw values: `sender` = `sender_handle`, `chat_name` = `display_name or chat_identifier`, dates as `apple_to_unix(...)`, attachments as `{"guid","mime_type","name","filename"}` (no `url` key — URLs are a caller concern), `link.image` = `image_url` (or `None` when only an embedded blob exists; fetch it with `db.link_image(rowid)`), `reply_to` = `ReplyTarget.to_dict()` = `{"guid", "text", "is_from_me", "sender_handle"}` (facts only, untruncated; the `or "Attachment"` / `[:120]` / `"You"` strings are `relay_reply`'s, §7). The adopter is told in the README that this is "the relay's shape without the relay's strings". The byte-identical shape lives only in the relay adapter (§7).

---

## 6. Internals that matter

### 6.1 Message SELECT (built once per `ChatDB` from the schema)

```sql
SELECT
    m.ROWID AS rowid, m.guid AS guid, m.text AS text,
    m.attributedBody AS attributed_body, m.date AS date, m.is_from_me AS is_from_me,
    m.handle_id AS handle_rowid,
    {date_read} {date_delivered} {date_edited} {date_retracted}                       -- optional -> NULL AS alias
    m.associated_message_guid AS assoc_guid, m.associated_message_type AS assoc_type,
    {associated_message_emoji AS assoc_emoji}
    m.cache_has_attachments AS has_attachments,
    {balloon_bundle_id}
    {CASE WHEN m.balloon_bundle_id = 'com.apple.messages.URLBalloonProvider' THEN m.payload_data END AS payload_data}
    {thread_originator_guid AS reply_to_guid} {thread_originator_part AS reply_to_part}
    {service} {item_type} {group_action_type} {group_title} {filter_action} {is_spam} {expressive_send_style_id}
    h.id AS sender, c.ROWID AS chat_rowid, c.guid AS chat_guid, c.display_name AS chat_name,
    c.chat_identifier AS chat_identifier, c.style AS chat_style
FROM message m
LEFT JOIN handle h ON m.handle_id = h.ROWID
JOIN chat_message_join cmj ON m.ROWID = cmj.message_id        -- LEFT JOIN when include_orphans
JOIN chat c ON cmj.chat_id = c.ROWID                           -- LEFT JOIN when include_orphans
```

The `CASE WHEN` trim is output-neutral because the relay only parses `payload_data` when `balloon_bundle_id == LINK_BALLOON`; it is skipped when `balloon_bundle_id` is absent from the schema (then `payload_data` is selected plain, and `Message.link` is parsed only if the column exists... concretely: no `balloon_bundle_id` column -> `link` is always `None`, matching the relay's gate).

Suffixes are the relay's verbatim: `WHERE m.ROWID > ? ORDER BY m.ROWID ASC`, `WHERE m.date_edited > ? ORDER BY m.date_edited ASC`, `WHERE c.guid = ? [AND m.ROWID < ?] ORDER BY m.ROWID DESC LIMIT ?`.

Statements kept verbatim in `sql.py` because their row order or row set is JSON-visible: `ATTACHMENTS_FOR` (no ORDER BY), `REPLY_TARGETS`, `LITE_MESSAGES`, `PARTICIPANTS` and `PARTICIPANTS_MAP` (no ORDER BY), `THREADS` (`GROUP BY c.ROWID ORDER BY last_rowid DESC LIMIT ?`), `CHAT_SERVICES` (two correlated subqueries), `UNREAD`, `LAST_ROWID`, `SEARCH`, `CHAT_ATTACHMENTS` (`ORDER BY m.date DESC`), `FIND_1TO1`, `FIND_GROUP_JOINS`, `FIND_GROUP_SENDERS`, `ONE_TO_ONE_ACTIVITY`, `PAYLOAD_FOR`, `ATTACHMENT_BY_GUID`.

### 6.2 `extract_text` and the 0x82 question

The byte-scan locator is the relay's; there are two changes, both recorded in the CHANGELOG as knowing deviations. (1) Length tag handling: `0x82` reads **4** bytes (relay: 3), `0x83` reads 8, any other tag `>= 0x80` returns `None` instead of being used as a raw length. Zero `0x82` rows exist (the largest text is under 10 KB), so the relay's output cannot change today. (2) Truncation: a blob cut before the end of its text — in the header, in the length tag or its length bytes, or inside the text itself — decodes to `None`, never a partial string; a blob cut only in the `0x86` trailer after the text (the text is complete) decodes to the complete text, exactly as an intact blob would. That is the §8.2 pin; the relay returned the partial decode. Every live blob carries the `0x86` trailer after its text, so this is reachable only on a corrupt blob. The full typedstream reader (shared-string table `0x92+i`, `0x84` new / `0x85` nil / `0x86` end, UTF-16 run lengths for the attribute table) was v0.2 scope and shipped in 0.2.0 as `typedstream_reader.py`, specified in `docs/TYPEDSTREAM.md`; `extract_text` is its oracle on every well-formed blob (tested; §14).

### 6.3 `parse_link_preview` (logic ported unchanged from the relay)

1. `KeyedArchive.load`; root must be a dict; if `root["richLinkMetadata"]` derefs to a dict, use it (Tahoe wrapper).
2. `url = string(root, "originalURL", "URL")`, `title = string(root, "title")`; neither -> `None`.
3. `has_embedded_image` = any `bytes` in `$objects` with `len > 3000` whose head is `\x89PNG`, `\xff\xd8`, or `ftyp` at offset 4.
4. `image_url` = `nested_url(root, "imageMetadata")`, else the first `http` string in `$objects` that is not the page URL (compare `split("?")[0].rstrip("/").lower()`) and contains none of `SKIP_IMG`. Computed **always** (the relay only computes it when there is no blob; the adapter reproduces precedence by checking `has_embedded_image` first, so the JSON is unchanged).
5. `summary = string(root, "summary")`, `site = string(root, "siteName")`.

The deref guard becomes a visited set with depth 32 (relay: depth 8, no visited set). A cyclic archive that the relay would have cut at depth 8 and treated as `None` is treated the same here (a cycle never resolves to a str/dict, so `None`). `embedded_images()` returns every qualifying blob in `$objects` order (`None` when the payload is empty, not a plist, or has no `$objects` list; `[]` when it parses but carries no blob) and `embedded_image()` returns the largest of them — the relay's `link_image` rule, with the three 404 states kept distinguishable for the adapter.

### 6.4 `chat_services` precedence

`service_family(out_svc) or service_family(any_svc) or service_family(chat_svc)` with the relay's statement verbatim (`IFNULL(m.service,'') <> ''`, `ORDER BY cmj.message_id DESC LIMIT 1`). The library function raises on SQL error; the relay wrapper keeps its `try/except -> {}` (judge 2 objection 9).

### 6.5 Search

`LIKE` stops at the first NUL in a typedstream blob, so `attributedBody` is searched with `instr(m.attributedBody, CAST(? AS BLOB)) > 0` for `(q, q.lower(), q.capitalize(), q.upper())`; every SQL hit is rechecked in Python against `clean_text(effective_text(...)).lower().find(q.lower())`, which drops false positives where the bytes occur in class names or attribute keys. Full scan; README says to scope with chat-level filtering or keep `limit` modest at 500k+ rows (FTS is impossible read-only).

### 6.6 Errors

`ChatDBError` base; `ChatDBAccessError` (FDA text names `sys.executable`, the Settings pane, and the Homebrew path-binding trap); `ChatDBBusy` (retryable); `CursorAhead(cursor_rowid, max_rowid)`; `SchemaError(column)`. Decoders never raise; `KeyedArchive.load` raises `ValueError` for callers who want it, `parse_link_preview` swallows it.

---

## 7. Relay integration plan (JSON stays byte-identical)

### 7.1 Where the adapter lives

Inline in `relay.py`, replacing the deleted chat.db block (not a separate module: the adapter needs `resolve`, `att_public`, `CONTACTS`, `SELF_RAW`, `person_key`, `group_title`, which live in `relay.py`; a separate file would import circularly). ~130 lines. Every dict literal is the old literal, key for key, in the old insertion order (FastAPI/`json.dumps` preserve insertion order, so order is part of "byte-identical").

### 7.2 The adapter block

```python
from imessage_chatdb import (ChatDB, Message, Attachment, ReplyTarget, LinkPreview, LINK_BALLOON,
                             apple_to_unix as apple_date_to_unix, extract_text as parse_attributed_body,
                             service_family as _norm_service, extract_urls, snippet)
from imessage_chatdb.chats import (find_chat as _lib_find_chat, chat_services as _lib_chat_services,
                                   participants as _lib_participants, last_rowid_for, lite_messages)

CDB = ChatDB(CHATDB, timeout=5, readonly_uri=False)   # today's connect; flip to True after the cmp gate passes

def db() -> sqlite3.Connection:        # same name: endpoints not yet ported keep `conn = db(); ...; conn.close()`
    return CDB.connect()

def relay_att(a: Attachment) -> dict:
    mime, name, url = att_public(a.guid, a.mime_type, a.transfer_name)
    return {"guid": a.guid, "mime_type": mime, "name": name, "url": url}

def relay_link(lp: LinkPreview | None, rowid: int):
    if lp is None:
        return None
    return {"url": lp.url, "title": lp.title, "summary": lp.summary, "site": lp.site,
            "image": f"/link_image/{rowid}" if lp.has_embedded_image else lp.image_url}

def relay_reply(r: ReplyTarget | None):
    if r is None:
        return None
    return {"text": (r.text or "Attachment")[:120],
            "sender": "You" if r.is_from_me else (resolve(r.sender_handle) or "")}

def relay_msg(m: Message) -> dict:     # was row_to_msg; same keys, same order
    return {
        "rowid": m.rowid, "guid": m.guid, "text": m.text,
        "date": apple_date_to_unix(m.date), "date_read": apple_date_to_unix(m.date_read),
        "date_edited": apple_date_to_unix(m.date_edited),
        "is_from_me": m.is_from_me,
        "sender": resolve(m.sender_handle), "sender_handle": m.sender_handle,
        "chat_guid": m.chat_guid,
        "chat_name": ((m.chat_display_name or m.chat_identifier) if m.is_group
                      else resolve(m.chat_identifier)),
        "is_group": m.is_group,
        "has_attachments": m.has_attachments,
        "assoc_guid": m.associated_guid, "assoc_type": m.associated_type,
        "attachments": [relay_att(a) for a in m.attachments],
        "link": relay_link(m.link, m.rowid),
        "reply_to_guid": m.reply_to_guid,
        "reply_to": relay_reply(m.reply_to),
        "service": m.service_raw,
    }

def fetch_new(cursor):            return [relay_msg(m) for m in CDB.messages_after(cursor)]
def fetch_edited(mark):
    msgs, new_mark = CDB.messages_edited_after(mark)
    return [relay_msg(m) for m in msgs], new_mark
def max_rowid():                  return CDB.max_rowid()
def max_date_edited():            return CDB.max_date_edited()
def fetch_thread_messages(chat_guid, limit, before_rowid):
    return [relay_msg(m) for m in CDB.thread_messages(chat_guid, limit=limit, before_rowid=before_rowid)]

def chat_services(conn, chat_rowids) -> dict:
    try:
        return _lib_chat_services(conn, chat_rowids)
    except Exception as e:
        print(f"[service] chat service lookup failed: {e}")
        return {}

def find_chat_for_addresses(conn, addrs):
    m = _lib_find_chat(conn, addrs, key=person_key, exclude=SELF_RAW)
    if not m:
        return None
    return {"chat_guid": m.chat_guid,
            "chat_name": (group_title(conn, m.chat_rowid, m.display_name) if m.is_group
                          else resolve(m.chat_identifier)),
            "last_rowid": m.last_rowid}
```

`group_title` keeps its body but reads `for hid in _lib_participants(conn, chat_rowid)`. `last_message_previews` keeps its `{"body","is_from_me","sender"}` output, `TAPBACK_VERBS`, the `2000..2005` / `3000..3005` branches, and the Photo/Video/paperclip strings; its SELECT + `attachments_for` are replaced by `lite_messages(conn, rowids)`, and the attachment branch calls `att_public(a.guid, a.mime_type, a.transfer_name)` first so a mime-NULL `.heic` still previews as "📷 Photo" (judge 2 objection 3).

### 7.3 Function-by-function replacement

| relay.py today (lines) | replacement | JSON-affecting detail preserved |
|---|---|---|
| `db()` 149-153 | `CDB.connect()` via the kept `db()` name | fresh connection per call; untouched endpoints keep `conn.close()` |
| `apple_date_to_unix` 156-165 | `imessage_chatdb.apple_to_unix` | identical expression -> identical floats; 0/None -> None |
| `parse_attributed_body` 168-186 | `extract_text` | deviations (§6.2): 0x82 = 4 bytes (no live rows); truncated blob -> `None`, not a partial string (corrupt rows only) |
| contacts 189-260 | **stays** | — |
| `MESSAGE_SELECT` 265-280 | `build_message_select(schema)` | same joins, same multiplicity, same ORDER suffixes |
| `_deref`, `parse_link_preview` 287-381 | `parse_link_preview` + `relay_link` | precedence embedded blob > imageMetadata > string scan, via `has_embedded_image` first |
| `row_to_msg` 384-409 | `row_to_message` + `relay_msg` | `effective_text`; `bool()`; `service_raw`; link only for `LINK_BALLOON` |
| `resolve_replies` 412-437 | `reply_targets` (inside `enrich`) + `relay_reply` | clean -> `or "Attachment"` -> `[:120]` -> `"You"`/resolve/`""`, in that order |
| `attachments_for` 440-460 | `attachments_for(include_plugin_payloads=False)` + `relay_att` | filter = `transfer_name` suffix (the couple of dozen hidden-not-plugin rows keep flowing) |
| `enrich` 463-469 | `enrich` (default in every fetch) | attachments only for `has_attachments` rows; one IN query each |
| `TAPBACK_VERBS`, `last_message_previews` 472-511 | **stays**, fed by `lite_messages` | strings unchanged; 2006 still falls through to text |
| `fetch_new` / `max_rowid` / `max_date_edited` / `fetch_edited` 514-556 | wrappers above | `enrich=True`; raw-int mark, `max` of returned rows, never regresses |
| `group_title` 559-573 | **stays**, reads `participants()` | first-name, 4 max, "…" |
| `last_rowid_for` 576-581 | `chats.last_rowid_for` | — |
| `person_key` 584-588 | **stays**, passed as `key=` | — |
| `find_chat_for_addresses` 591-649 | `find_chat(key=person_key, exclude=SELF_RAW)` + wrapper | 1:1 iff `len(addrs)==1`; tie-break kept |
| `icon_known_missing` 652-656 | **stays** | — |
| `_norm_service` / `chat_services` 659-702 | `service_family` / wrapper with try/except | `{}` on failure |
| `mac_thread_labels` 705-713 | **stays** | — |
| `fetch_threads` 716-800 | Phase 3: `CDB.chats(limit)`, `participants_map()`, `lite_messages`, `chat_services`, `unread_count` | reads/pins/archived/auto_translate/icon/FORCED_UNREAD/baseline logic unchanged |
| `fetch_thread_messages` 803-814 | wrapper above | DESC LIMIT then reversed |
| `poll_loop` 1023-1059 | same four calls; add `except CursorAhead` -> re-init cursor at `max_rowid()`, log once | broadcast/push rules unchanged |
| `search_messages` 1465-1522 | Phase 2: `search(conn, q, limit=limit)` + relay `snippet`/`title`/`who` | `len(q) < 2` guard stays in relay; `limit*2` oversample |
| `link_image` 1641-1663 | Phase 2: `p = CDB.payload(rowid)`; `not p` -> 404 "no payload"; `blobs = embedded_images(p)`; `blobs is None` -> 404 "unparseable payload"; `not blobs` -> 404 "no embedded image"; `best = max(blobs, key=len)`; relay `sniff_image` | all three 404 bodies stay (`CDB.link_image` alone collapses them into one `None`; `KeyedArchive.load` is **not** used here because it rejects a missing `$top` that the relay tolerates) |
| `/attachment` 1955-2009, `/thumbnail` 2012-2046 | Phase 2: `CDB.attachment(guid)` -> `a.path`, `a.mime_type`, `a.transfer_name` | HEIC/sips/qlmanage/caches/`FAILED_HEIC` unchanged |
| `thread_media` 2049-2096 | Phase 2: `chat_attachments(chat_guid)` + `att_public` + `apple_date_to_unix(a.message_date)`; `recent_messages(chat_guid, 1000)` + `extract_urls` + relay dedupe/"me"/resolve | attachments `ORDER BY m.date DESC`; links newest-first, first-seen dedupe |
| `contact_recency` ~1205 | optional: `one_to_one_activity()` | — |
| `match_chat`, `create_chat`, assistant/prepare | unchanged (call the wrappers) | — |

### 7.4 Why the JSON is byte-identical (the argument, then the proof)

Argument: same SQL text and join shape (same rows, same order, including the statements without `ORDER BY`); `apple_to_unix` is the same expression on the same raw ints; the adapter dicts are the old literals in the old order; every string-producing step (`resolve`, "You", "me", first names, verbs, emoji, `att_public`, `/link_image/`, HEIC advertising) is still relay code; `effective_text` reproduces `row_to_msg` for `""`/`None`; `bool()` is applied in `row_to_message`; the reply fallback and truncation order is reproduced in `relay_reply`; the attachment filter is the same suffix test; link-image precedence is reproduced by checking `has_embedded_image` first; the edit mark stays a raw Apple int.

Proof, three gates, all must pass before the LaunchAgent is restarted:

1. **Committed unit gate** — `<relay>/tests/test_relay_compat.py` + `<relay>/legacy_chatdb.py` (verbatim copy of relay.py 147-186, 265-511, 514-556, 576-581, 591-649, 659-702, 803-814, `search_messages`, `link_image`'s selection loop, `thread_media`). The test imports `relay` with stub env (placeholder token values that are never the real ones; no `.env` read) and `legacy_chatdb`, points both at a synthetic database built with the library's test builders (copied into the relay's `tests/`), seeds `CONTACTS` with synthetic names, and asserts `json.dumps(old, ensure_ascii=False) == json.dumps(new, ensure_ascii=False)` for `fetch_new(0)`, `fetch_edited(0)`, `fetch_thread_messages` (with and without `before_rowid`), `fetch_threads(200)` (Phase 3), `search_messages`, `thread_media`, `find_chat_for_addresses` (1:1, group, self-included, no match), `last_message_previews`. The synthetic DB covers: text column vs blob-only, `""` text with blob, reply to attachment-only target, reply > 120 chars, URL balloon with embedded image / with `imageMetadata` only / with scan-only, HEIC attachment with NULL mime, plugin payload attachment, hidden-not-plugin PNG, each tapback 2000-2006/3000-3002/1000, a 2006 emoji row, iMessageLite/SMS/RCS messages, a group with join rows missing for one sender, a `date_edited` bump.
2. **Live shadow gate** — `<relay>/tools/shadow_diff.py` (committed; output never committed) runs `legacy_chatdb` and the new path read-only on the live DB: `fetch_new(max_rowid-500)`, `fetch_edited(0)` limited to the last 10k rows, `fetch_threads(200)`, `fetch_thread_messages` for the top 20 chats x 50 (+ one `before_rowid` page each), `search` for `["the","http","ok"]`, `thread_media` for the top 5, link previews for every URL-balloon row, `find_chat_for_addresses` for 10 address sets taken from existing chats. It compares `json.dumps(..., ensure_ascii=False)` bytes and prints **counts and mismatching ROWIDs only**, never text, handles, or names. Zero mismatches gates the switch.
3. **HTTP gate** — the relay's existing before/after API diff procedure (captures of `/threads`, `/thread/{guid}/messages`, `/search`, `/thread/{guid}/media`, `/link_image/{rowid}` taken before the edit and after the LaunchAgent restart, diffed byte-for-byte, then deleted because they contain real messages). Zero diff lines.

### 7.5 Cutover order

1. `<relay>/venv/bin/pip install -e <this repo>` (relay venv is 3.14; library requires >= 3.12).
2. Copy the legacy functions to `legacy_chatdb.py`; back up `relay.py` per the directory's existing `relay.py.pre-*` convention.
3. Phase 1 edit (core block + `poll_loop` `CursorAhead` handler), `readonly_uri=False`.
4. Gates 1 and 2. Then restart only the relay LaunchAgent (BlueBubbles, Open WebUI, Docling untouched). Gate 3.
5. Flip `readonly_uri=True`; re-run gates 2 and 3.
6. Phase 2 (endpoints), Phase 3 (`fetch_threads`): each followed by gates 1-3.
7. One release later: delete `legacy_chatdb.py`; keep `test_relay_compat.py` running against recorded golden JSON from the synthetic DB (so regressions in the relay stay caught after the legacy code is gone).

Deliberately unchanged in Phase 1 (each becomes an opt-in, versioned JSON change later): iMessageLite labelled as iMessage family; 2006 emoji tapbacks previewed via their text; the no-op `startswith("iMessage")` tie-break; attachments/participants without `ORDER BY`; orphan rows excluded; `date_retracted` not surfaced.

Never printed, logged, or committed anywhere in this plan: the relay token, anything from `<relay>/.env` or the LaunchAgent plist, message text, handles, names.

---

## 8. Test plan (synthetic fixtures only)

`conftest.py` asserts every database path is under `tmp_path` and refuses anything under `~/Library`. CI (GitHub Actions, `macos-latest` + `ubuntu-latest`, Python 3.12 and 3.14) has no Messages database; nothing in the library needs macOS except the default path.

### 8.1 Fixture databases

`fixtures/schema_profiles.py` has four DDL subsets with the real key structure (`ROWID INTEGER PRIMARY KEY AUTOINCREMENT`, `guid TEXT UNIQUE NOT NULL`, `chat_message_join (chat_id, message_id) PRIMARY KEY`, `chat_handle_join UNIQUE(chat_id, handle_id)`, `message_attachment_join UNIQUE(message_id, attachment_id)`):

- `macos14`: no `associated_message_emoji`, `filter_action`, `date_retracted`, `date_updated`, `thread_originator_part`, `chat.is_filtered`.
- `macos15`: + `thread_originator_part`.
- `macos26`: + `associated_message_emoji`, `filter_action`, `date_retracted`, `chat.is_filtered`.
- `macos27`: + `date_updated`, `is_spam`, `group_title`.

`make_db(tmp_path, profile)` creates the file with `PRAGMA journal_mode=WAL` and yields `(writer_conn, ChatDB)`. `builders.py`: `add_handle(id, service)`, `add_chat(guid, style, identifier, display_name=None, service_name=None, handles=())` (guids `any;-;...` / `any;+;...`), `add_message(chat_rowid, *, text=None, body=None, date_ns=BASE+n, is_from_me=0, handle=None, assoc_guid=None, assoc_type=0, assoc_emoji=None, reply_to=None, balloon=None, payload=None, service="iMessage", has_att=0, date_edited=0, date_read=0, join=True)`, `add_attachment(msg_rowid, guid, mime, transfer_name, filename, *, hide=0, sticker=0)`. Synthetic handles only (`+15550001234`, `test@example.invalid`). Dates are Apple ns from a fixed base; one case writes seconds for the heuristic.

### 8.2 Fabricating `attributedBody`

`fixtures/typedstream_writer.py::encode_attributed_body(text, *, mutable=False, force_len_tag=None) -> bytes`. Two skeletons, both verified byte-for-byte against the live layouts by the judges:

- plain: `b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"`
- mutable: `b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x0fNSMutableString\x01\x84\x84\x08NSString\x01\x95\x84\x01+"`

then LEN then the UTF-8 bytes then the trailer `b"\x86\x84\x02iI"` + run length (UTF-16 units, same int encoding) + attr count + `b"\x92\x84\x84\x84\x0cNSDictionary\x00\x94\x84\x01i\x01\x92\x84\x96\x97\x1d__kIMMessagePartAttributeName\x86\x92\x84\x84\x84\x08NSNumber\x00\x84\x84\x07NSValue\x00\x94\x84\x01*\x84\x99\x99\x00\x86\x86\x86"`. LEN: one byte `< 0x80`; `0x81` + u16le; `0x82` + u32le; `0x83` + u64le; `force_len_tag` emits a wider tag for a short string. Golden assertions: output starts `040b73747265616d7479706564 81e803`; the byte after the string is `0x86` followed by `84 02 69 49`.

Pinned: lengths 0/1/127/128/65535/65536 (a 70,000-byte text forces `0x82`; the decoded last byte is intact and there is no leading NUL), forced `0x83`, emoji/Hebrew (length is bytes, not code points), both skeletons, `""` -> `None`, no `NSString` -> `None`, every truncation prefix handled without raising — a prefix that cuts the header, the length tag or its length bytes, or the text -> `None` (never a partial string); a prefix that only cuts the `0x86` trailer, so the text is complete -> the complete text, invalid UTF-8 -> U+FFFD, an unknown tag `0x80`/`0x90` -> `None`, `effective_text` for the four combinations of `""`/`None` x blob/no blob (including `""` + blob whose decode fails -> `None`, the relay's result).

### 8.3 Fabricating `payload_data`

`fixtures/keyed_archive_writer.py::make_link_payload(url=None, title=None, summary=None, site=None, *, wrapped=False, embed=None, image_meta_url=None, extra_strings=(), cyclic=False) -> bytes` builds `{"$archiver": "NSKeyedArchiver", "$version": 100000, "$top": {"root": UID(1)}, "$objects": ["$null", root, ...]}` with `plistlib.UID` pointers; archived `NSURL` as `{"$class": UID(k), "NS.base": UID(0), "NS.relative": UID(j)}`; `wrapped=True` adds an outer RichLink dict whose `richLinkMetadata` points at the inner `LPLinkMetadata` (Tahoe shape); `embed` inserts a PNG (`\x89PNG\r\n\x1a\n` padded), JPEG (`\xff\xd8\xff`) or ISOBMFF (`....ftypheic`) blob of a given size (3,100 default; a 2,000-byte decoy case); `image_meta_url` adds `imageMetadata: {"URL": UID(nsurl)}`; `cyclic=True` makes a UID point at its own container; serialized with `plistlib.dumps(fmt=plistlib.FMT_BINARY)`.

Pinned: flat == wrapped; `originalURL` > `URL`; `NS.relative` unwrapping; url-only / title-only accepted, neither -> `None`; `has_embedded_image` only for > 3000 bytes and the three magic heads; `image_url` precedence `imageMetadata` > scan, scan skips the page URL (query and trailing slash stripped, case-insensitive) and `SKIP_IMG`; `embedded_image` returns the largest blob; cyclic / non-plist / string root / out-of-range UID -> `None` without raising.

### 8.4 Other pinned behaviours

- dates: `0`/`None` -> `None`; seconds and ns inputs; equality with the literal relay expression; `unix_to_apple` round trip; `apple_to_datetime` UTC-aware.
- reactions: every 2000-2006 / 3000-3006 / 1000 / unknown; all three guid forms and `None`; `Message.reaction is None` for plain rows.
- services: four raw values + unknown + `None`; `service_family` equals the relay's `_norm_service` on every input.
- schema: all public functions run on all four profiles; missing optional -> `None`; `date_edited` absent -> `([], mark)` and `max_date_edited() == 0`; a profile missing `message.guid` -> `SchemaError("message.guid")`; `build_message_select` substitutes `NULL AS`.
- connection: `INSERT` raises (`query_only`); URI quoting for a path containing a space and `%`; `readonly_uri=False` works; fixture file sha256 unchanged after the whole suite; a WAL write from the writer connection is visible to a `mode=ro` reader and invisible to an `immutable=1` reader (documents why immutable is forbidden); `BEGIN EXCLUSIVE` on a rollback-journal fixture with `timeout=0.05` -> `ChatDBBusy`; a nonexistent path -> `ChatDBAccessError` whose message contains `sys.executable`.
- messages: strict `> rowid`, ASC, `limit`; `thread_messages` DESC-then-reversed with `before_rowid` paging; `recent_messages` newest-first; `enrich` issues exactly one attachments query and one reply query per batch (sqlite3 `set_trace_callback`) and only for `has_attachments` rows; `is_from_me`/`has_attachments` are `bool`; `link` parsed only for `LINK_BALLOON`; `text` rule; orphans excluded by default and included with `include_orphans`; `reply_targets` gives cleaned untruncated text and `""` for attachment-only targets.
- edits: only rows past the mark, ASC by `date_edited`, returned mark is the max raw int, never regresses; an in-place edit the ROWID cursor misses is caught.
- attachments: suffix filter default and override; the hidden-not-plugin PNG is returned by default; `path` expanded, `None` for NULL filename; `exists()`; `chat_attachments` order by `m.date DESC`.
- chats: `chats_by_activity` excludes empty chats, orders by `last_rowid DESC`, limit; `participants` / `participants_map`; `chat_services` precedence incl. empty-string services ignored and Lite folded; `unread_count` incoming-only; `lite_messages` attachments only for `has_attachments`.
- find_chat: 1:1 via custom `key` collapsing phone+email; `exclude`; two addresses that collapse to one person still take the **group** branch; group = joins UNION senders, exact set equality, newest wins, tie-break; no match -> `None`.
- search: text-column hit, blob-only hit with a NUL before the text, case variants, recheck drops an attribute-key false positive, tapbacks excluded, `limit`/oversample, `snippet` windows, `extract_urls` trailing-punctuation stripping.
- watch: 3 inserts -> 3 `new` events in order; edit bump -> `edited` with a raw-int mark; idle polls -> none; `cursor=None` starts at now (no replay); `new` before `edited` within a round; busy -> `([], cursor)`; rebuilt DB -> `CursorAhead`; fake `sleep` + stop `Event`; `run_watch` calls `on_cursor` after each advance; `Cursor.to_json` round trip.
- CLI: each subcommand prints parseable JSON lines against a fixture path; `check` reports the profile.
- fuzz: 2,000 seeded iterations each of random bytes, bit-flipped valid blobs, and plists with random UIDs into `extract_text`, `parse_link_preview`, `embedded_image`: only a value or `None` is acceptable.

Tooling: `ruff`, `mypy --strict` on `src`, `pytest -q`.

---

## 9. Packaging

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "imessage-chatdb"
version = "0.1.0"
description = "Read-only, stdlib-only reader for Apple Messages chat.db: messages, edits, tapbacks, replies, link previews, attachments, chats, search, polling."
readme = "README.md"
license = "MIT"
requires-python = ">=3.12"
dependencies = []
classifiers = ["Programming Language :: Python :: 3", "Operating System :: MacOS", "License :: OSI Approved :: MIT License", "Typing :: Typed"]

[project.optional-dependencies]
dev = ["pytest", "ruff", "mypy"]

[project.urls]
Homepage = "https://github.com/Avtraang/imessage-chatdb"

[tool.hatch.build.targets.wheel]
packages = ["src/imessage_chatdb"]

[tool.hatch.build.targets.sdist]
exclude = ["/DESIGN.md"]      # design notes live in the repo, not in the distribution
```

`src/imessage_chatdb/py.typed` ships. `CHANGELOG.md` 0.1.0 records the 0x82 deviation. No relay backups, state files, tokens, or real data may ever enter the repo (`.gitignore` covers `*.db`, `*.db-wal`, `*.db-shm`, `relay_state*`, `.env`).

---

## 10. README outline

1. What it is / what it is not (read-only; no sending; no contacts; macOS only by default path).
2. Install; Full Disk Access in 30 seconds (`python -m imessage_chatdb check`; the grant is per interpreter binary; the Homebrew upgrade trap).
3. Quickstart: open, last 20 messages, tail with `watch()`, persist the `Cursor`.
4. Concepts: ROWID cursor (at-least-once), edits via `date_edited`, Apple epoch and the 0 sentinel, `text` vs `attributedBody`, tapbacks (incl. 2006 emoji), replies, link previews, attachments and the plugin-payload filter, services incl. iMessageLite, group vs 1:1, chat matching with your own `key`, search (NUL-safe `instr`), schema tolerance and profiles, orphans, `CursorAhead`.
5. Recipes: a bot (reply to incoming non-tapbacks), an exporter (attachments of one chat), a bridge (asyncio + `to_thread(db.poll)`).
6. Data model reference (one table per dataclass).
7. Performance notes and limits (full-scan search, `chats()` GROUP BY, 500k+ rows).
8. Safety: never `immutable=1`; why every query is `fetchall()`-ed; thread rules.
9. Compatibility matrix (macOS 14-27 profiles) and known deviations (0x82).
10. Contributing: synthetic fixtures only; never commit a real database.

---

## 11. Ordered implementation task list (sized for parallel agents)

Each task names the files it owns; no two tasks own the same file. "Done" always includes `ruff` and `mypy --strict` clean and tests green on all four profiles where applicable.

**Wave 0 (sequential, one agent) — T0 Scaffold.** Owns `pyproject.toml`, `LICENSE`, `CHANGELOG.md`, `.gitignore`, `src/imessage_chatdb/__init__.py` (stub re-exports with `__all__`), `py.typed`, `errors.py`, `tests/conftest.py`, `tests/fixtures/schema_profiles.py`, `tests/fixtures/builders.py`. Inputs: §3, §8.1, §9, §6.6. Done: `pip install -e .` works; `make_db(tmp_path, profile)` builds all four profiles in WAL mode; a smoke test inserts a chat and a message per profile; conftest refuses `~/Library`.

**Wave 1 (parallel, after T0):**
- **T1 dates + services + handles + reactions.** Owns `dates.py`, `services.py`, `handles.py`, `reactions.py`, `test_dates.py`, `test_services.py`, `test_reactions.py`. Inputs: §4.3, relay.py 156-165 and 659-667 (verbatim expressions). Done: pinned behaviours in §8.4 (dates, reactions, services) pass; `service_family` equals the relay's `_norm_service` on a parametrised table.
- **T2 typedstream.** Owns `typedstream.py`, `tests/fixtures/typedstream_writer.py`, `test_typedstream.py`. Inputs: §6.2, §8.2, relay.py 168-186. Done: both skeletons produce the golden header/trailer bytes; §8.2 pins pass; the 70,000-byte case decodes with the last byte intact.
- **T3 keyed archive + link preview.** Owns `keyed_archive.py`, `link_preview.py`, `tests/fixtures/keyed_archive_writer.py`, `test_link_preview.py`. Inputs: §6.3, §8.3, relay.py 283-381 and 1641-1663. Done: §8.3 pins pass; cyclic archive returns `None` without recursion error.
- **T4 schema + sql + connection.** Owns `schema.py`, `sql.py`, `connection.py`, `test_schema.py`, `test_connection.py`. Inputs: §4.1, §4.2, §6.1, the verbatim statement list, relay.py 149-153, 265-280, 419-423, 444-448, 480-486, 563-565, 578-580, 606-607, 616-621, 632-637, 686-696, 719-727, 735-736, 776-779, 1480-1497, 2053-2063. Done: `build_message_select` renders `NULL AS` for every optional column on `macos14`; required-column `SchemaError`; WAL-vs-immutable, `BEGIN EXCLUSIVE`, URI quoting, sha256 tests pass; every `sql.py` constant is byte-equal to the relay's statement text (a test loads the strings from a `tests/fixtures/relay_sql_golden.py` copied once from relay.py).
- **T5 models.** Owns `models.py`, `test_models.py`. Inputs: §5, §5.1. Done: all dataclasses frozen+slotted; properties covered; `to_dict()` key order equals the documented relay key list; `bool` fields are `bool`.

**Wave 2 (parallel, after wave 1):**
- **T6 messages + attachments.** Owns `messages.py`, `attachments.py`, `test_messages.py`, `test_edits.py`, `test_attachments.py`. Inputs: §4.4, §6.1, relay.py 384-556, 803-814, 2053-2073. Done: §8.4 messages/edits/attachments pins pass on all profiles; trace-callback query-count tests pass; hidden-not-plugin PNG is returned by default.
- **T7 chats + find_chat + search.** Owns `chats.py`, `search.py`, `test_chats.py`, `test_find_chat.py`, `test_search.py`. Inputs: §4.4, §6.4, §6.5, relay.py 559-702, 716-800 (SQL only), 1465-1522, 2077-2093. Done: §8.4 chats/find_chat/search pins pass; the "two addresses, one person" case takes the group branch; `chat_services` raises on a dropped table (the relay wrapper is what swallows it).

**Wave 3 (parallel, after wave 2):**
- **T8 db facade + watch.** Owns `db.py`, `polling.py`, `test_watch.py`, and fills `__init__.py` exports. Inputs: §4.1, §4.5, §4.6. Done: one connection per call (trace shows open/close per method); `poll`/`watch`/`run_watch` pins pass incl. busy and `CursorAhead`; `open()` convenience works.
- **T9 CLI + fuzz + README + CHANGELOG.** Owns `__main__.py`, `test_cli.py`, `test_fuzz.py`, `README.md`, `CHANGELOG.md` content. Inputs: §4.7, §8.4 fuzz, §10. Done: JSON-lines output parses; fuzz passes; README sections 1-10 present with runnable snippets checked by a doctest-style smoke test against a fixture.

**Wave 4 (sequential, in the relay repo, after T8; requires the owner's OK before any edit to `relay.py` or a LaunchAgent restart):**
- **T10 legacy copy + compat test.** Owns `<relay>/legacy_chatdb.py`, `<relay>/tests/test_relay_compat.py`, `<relay>/tests/fixtures/` (builders copied from the library). Inputs: §7.4 gate 1. Done: the test passes against the **current** relay (legacy vs legacy) before any relay edit, proving the harness; env stubs only.
- **T11 Phase 1 relay edit.** Owns the chat.db block of `<relay>/relay.py` (147-186, 265-556, 576-581, 591-649, 659-702, 803-814) and `poll_loop`'s `CursorAhead` handler. Inputs: §7.2, §7.3. Done: gate 1 green; `readonly_uri=False`.
- **T12 shadow diff + cutover.** Owns `<relay>/tools/shadow_diff.py`. Inputs: §7.4 gates 2-3, §7.5. Done: zero mismatches; LaunchAgent restarted (relay only); HTTP gate zero diff; then flip to `readonly_uri=True` and repeat gates 2-3.
- **T13 Phase 2 endpoints, T14 Phase 3 `fetch_threads`.** Own their respective relay functions. Done: gates 1-3 green after each.
- **T15 cleanup (one release later).** Delete `legacy_chatdb.py`; switch `test_relay_compat.py` to recorded golden JSON from the synthetic DB.

---

## 12. Risks

1. Byte identity rests on verbatim SQL, verbatim `apple_to_unix`, adapter key order, and reproduced quirk order (`Attachment` fallback before `[:120]`; `""`/`None` text; `bool()`; suffix filter). Three gates guard it; any future improvement is a deliberate, versioned JSON change.
2. 0x82 (4-byte) and 0x83 reads, and the truncated-blob -> `None` rule, are exercised only synthetically; the old reading was wrong for such rows anyway.
3. Implicit scan order where the relay has no `ORDER BY` (attachments, participants) is preserved by verbatim SQL; a planner change could reorder — Phase 2+ may add explicit ordering as a versioned change.
4. `mode=ro` needs an openable `-shm`; sandboxed callers get `ChatDBAccessError`. The relay starts on `readonly_uri=False` and flips after the gates.
5. Full Disk Access is path-bound to the interpreter; the library can only explain.
6. Cursor edges: rebuilt DB (`CursorAhead`), iCloud history downloads (old dates, new rowids — filter on `message.date`), edits synced with an older `date_edited` (missed; `date_updated` is a candidate universal mark, unverified, v0.2 experiment).
7. Orphan rows (a few hundred) are hidden by the inner join; `include_orphans` is the escape hatch; Messages.app appears to write both rows in one transaction.
8. Performance: `chats()` GROUP BY and `search`'s four `instr()` scans are O(messages); fine at 20k, documented for 500k+.
9. Privacy: tests never touch real data; shadow diff prints counts/rowids only; the library never logs content; the repo contains no backups, state, or tokens.
10. Scope discipline: v0.2 items (typedstream reader, parts, edit history, retraction events, `date_updated`) are listed so they do not creep into v0.1. *(0.2.0 shipped the reader, parts and retraction events — §14; edit history and `date_updated` remain open.)*

---

## 13. Objection ledger (every judge objection, disposition)

| # | Objection (judge) | Disposition |
|---|---|---|
| 1 | `hide_attachment` ≈ plugin payloads + stickers is wrong; `include_hidden` would drop a couple of dozen live attachments (J1-1, J2-1) | **Accepted.** Default exclusion is `transfer_name.endswith(".pluginPayloadAttachment")` only; parameter is `include_plugin_payloads`; `Attachment` exposes both `is_plugin_payload` and `hide_attachment`; a fixture with a hidden-not-plugin PNG pins it. |
| 2 | P1's text rule and P2's `message_text` both diverge from `row_to_msg` on `""` edges (J1-2) | **Accepted.** `effective_text` is P3's statement verbatim; four-way `""`/`None` x blob test incl. failed decode -> `None`. |
| 3 | P3's `message_json` emits `reply_to.text` without `or "Attachment"` / `[:120]` (J1-3, J2-5) | **Accepted.** `ReplyTarget.text` is cleaned and untruncated; `relay_reply` does `(r.text or "Attachment")[:120]`, the relay's order. |
| 4 | `bp:` and bare-GUID counts misstated (J1-4, J2-7) | **Accepted.** §2 records bp: a few dozen (some emoji), bare: a handful; parser handles all three forms. |
| 5 | "message in two chats happens" unverified (J1-5) | **Accepted.** Claim removed; 0 such rows; SQL semantics unchanged (would yield one object per join row). |
| 6 | P1 folds `date_retracted` into the edit mark with unspecified ordering (J1-6, J2-11) | **Accepted.** No folding; retraction events are out of v0.1; `date_retracted` exposed as a field only. |
| 7 | Previews via `hydrate=True` add work and move strings into the library (J1-7, J2-3) | **Accepted.** `lite_messages` returns facts with attachments only for `has_attachments` rows; all preview strings and the `att_public`-first HEIC rule stay in the relay. |
| 8 | 1:1 branch criterion unstated (J1-8, J2-4) | **Accepted.** Pinned to `len(addresses) == 1`, with a test for two addresses collapsing to one person taking the group branch. |
| 9 | P2's `fetch_new`/`fetch_edited` don't say whether they enrich (J1-9) | **Accepted.** `enrich=True` default on `messages_after`, `messages_edited_after`, `thread_messages`, `message`; `recent_messages` defaults to `False` (link scan only). |
| 10 | `mode="pragma"` should not be a documented library mode (J1-10) vs keep it for the cutover (J2 graft) | **Both honoured.** `readonly_uri=False` exists, is labelled a compatibility shim in the docstring and README, is not in the quickstart, and the relay flips to `True` after the gates. |
| 11 | `CursorAhead` escapes `poll_once` into the relay's generic `except` -> log spam (J1-11) | **Accepted.** Library raises (adopters should know); the relay's `poll_loop` gets an explicit `except CursorAhead` that re-initialises at `max_rowid()` and logs once. |
| 12 | P3 v0.1 scope is a maintainer liability; parts tests are self-consistent only (J1-12) | **Accepted.** Typedstream reader, parts API, edit history, retraction events moved to v0.2; v0.1 is the byte-scan with golden header/trailer bytes as ground truth. |
| 13 | `bool()` of `is_from_me`/`has_attachments` unstated (J1-13, J2-6) | **Accepted.** Fields typed `bool`; `row_to_message` applies `bool()`; test asserts `is True`/`is False`. |
| 14 | P1 replaces `db()` with thread-local connections while endpoints call `conn.close()` (J2-2) | **Accepted.** One fresh connection per call; `db()` returns `CDB.connect()`; untouched endpoints keep working. |
| 15 | P3 makes `service`, `balloon_bundle_id`, `payload_data` required (J2-8) | **Accepted.** All three optional; `link` is `None` when `balloon_bundle_id` is absent. |
| 16 | P2's `chat_services` wrapper loses the relay's `try/except -> {}` (J2-9) | **Accepted.** Library raises; relay wrapper keeps the `try/except` and the log line. |
| 17 | P2's compat evidence is an uncommitted one-off (J2-10) | **Accepted.** Committed `test_relay_compat.py` (gate 1) + committed `shadow_diff.py` with uncommitted output (gate 2) + the existing HTTP diff (gate 3). |
| 18 | P1's `edited_messages(include_retracted)` ordering undefined (J2-11) | **Accepted.** Parameter removed (see 6). |
| 19 | 0x82 4-byte read is a real deviation (J1-14, J2-12) | **Acknowledged, kept.** Spec-correct, zero live rows, recorded in CHANGELOG and §7.5. |
| 20 | P1's presentation helpers (`one_line_summary`, `Tapback.verb`, `format_group_title`) leak strings (J1 graft a) | **Accepted.** Dropped. `Reaction.kind` is a lowercase token, not a verb. |
| 21 | Thread-local autocommit connections (P1) vs per-call (P2/P3) | **Per-call** (see 14); `connection()` context manager for batching; `isolation_level=None` and `fetchall()` everywhere so no snapshot lingers. |
| 22 | P3's `date_updated` reliance | **Rejected for v0.1.** Semantics unverified; listed as a v0.2 experiment. |
| 23 | P1's `find_chat` `key=canonical_address` default | **Kept as `address_key`** (digits, last 10 when >= 10; email lowercased) — matches the relay's `norm_key` for non-contact inputs; the relay passes `person_key`. |

---

## 14. Addendum — what 0.2.0 shipped (2026-10-06)

Everything above describes v0.1 as designed; this section records the v0.2
scope as it landed. The rule for the release was **additive**: every 0.1.x
call keeps its signature, defaults and output (`extract_text` /
`effective_text` untouched, every relay-pinned statement byte-identical,
`to_dict()` unchanged, the CLI byte-identical without a new flag, and the
default event stream the 0.1 stream). `CHANGELOG.md` 0.2.0 is the user-facing
statement; the design points are:

### 14.1 Typedstream reader (`typedstream_reader.py`, spec `docs/TYPEDSTREAM.md`)

- A separate module, as §0 planned; `typedstream.py` is not touched and
  `extract_text` is the oracle (`parse_attributed_body(b).text ==
  (extract_text(b) or "")` on every writer-produced blob). Two layers: the
  generic typedstream grammar (`_Reader`: header, integers, the two shared
  tables, typed-value groups, literal objects and references) and the
  `NSAttributedString` interpretation (`parse_attributed_body`: text, `iI`
  run pairs with the dictionary-index rule, attribute value conversion,
  `message_parts`). Public surface: `TypedStreamError`, `AttributedBody`,
  `AttributeRun` (UTF-16 offsets; `chars()` converts), `Url`, `UnknownValue`,
  `Mention`, `TextPart`, `AttachmentPart`, `UnknownPart`, `Part`,
  `parse_attributed_body`, `try_parse_attributed_body`, `message_parts`, all
  in `__all__`.
- **Hardening rules (fixed after the first 0.2 review):** every depth is a
  counter, never the interpreter's recursion limit — literal object nesting
  (64), class-chain length (64, read iteratively, references included) and
  the nesting of *converted* values (64 mappings, counted through
  back-references, since a reference DAG can nest deeper than any literal).
  Each archived object is converted once and cached on it, so conversion is
  linear in time and memory however many runs or pairs reference one object.
  `message_parts` converts all run offsets in one pass (linear in text +
  runs). In value position a known class with an unexpected layout becomes
  `UnknownValue` like an unknown class; the text string, a key and a run
  dictionary stay strict. `try_parse_attributed_body` additionally catches
  `RecursionError` as a backstop. `test_fuzz.py` feeds the reader the same
  never-raise corpus as the other decoders.
- The 0.1 fixture writer `encode_attributed_body` is kept byte-for-byte for
  the 0.1 pins; the reader rejects its blobs (three trailer discrepancies,
  `docs/TYPEDSTREAM.md` §7). `encode_attributed_body_runs` is the byte-exact
  archiver the reader is tested against.
- `Message.attributed_body_raw` (keyword-only, default `None`, filled by
  `row_to_message`), `Message.body` and `Message.parts` (cached in private
  non-compared slots; never raise). `to_dict()`, `==`, `hash()`, `repr()`
  unchanged. CLI: `tail --parts` / `search --parts` append a `"parts"` key.

### 14.2 Retraction events (`polling.py`, `messages.py`, `sql.py`)

- `Cursor.retract_mark` (third field, default 0; `to_json()` writes it,
  `from_json()` reads a missing key as 0). `Event.kind` gains `"retracted"`;
  within a round `"new"`, then `"edited"`, then `"retracted"` events, with the
  same per-event cursor and tie rules as edits (§4.6) — the retracted events
  carry the round's final edit mark and a running retract mark, so a resume
  from an edited event's cursor replays all the round's unsends.
- The columns are **not** folded into one mark (objection 6 stands):
  `messages_retracted_after` / `max_date_retracted` are the edit statements
  with `date_retracted` substituted, pinned as derived statements.
- **Opt-in** (`include_retractions=False` by default on `poll_once`, `watch`,
  `run_watch`, `ChatDB.poll`, `ChatDB.watch`): a 0.1 consumer written as
  `if kind == "new": ... else: apply_edit(...)` must not receive a third kind
  it does not know. While off, the mark does not move, so opting in later
  delivers the missed unsends; `initial_cursor()` seeds the mark regardless.
- `chats_changed_since` / `ChatActivity` (§4.3/§5 above) are the other 0.2
  additions; `search(..., chat_guid=)` was 0.1.1.

### 14.3 Still open

Edit history from `message_summary_info`, `date_updated` as a universal
change mark (unverified), FTS, retiring `encode_attributed_body`
(test-only). A CLI switch for retraction events in `tail --follow` was not
added (the CLI stays byte-identical to 0.1.1 without a new flag).
