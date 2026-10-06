# Changelog

All notable changes to `imessage-chatdb` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## 0.2.0 — 2026-10-06

An additive release: **nothing a 0.1.x caller does changes.** Every 0.1
function, method and CLI command keeps its signature, its defaults and its
output. `extract_text` / `effective_text` are the 0.1 functions untouched,
every SQL statement pinned against the relay is byte-identical, `to_dict()`
emits the same keys in the same order, `tail` / `search` print the same lines
unless the new `--parts` flag is given, and the event stream of `poll_once`,
`watch`, `run_watch`, `ChatDB.poll` and `ChatDB.watch` carries the same two
kinds (`"new"`, `"edited"`) unless the new `include_retractions=True` keyword
is passed. The one addition visible in existing output is the `retract_mark`
key that `Cursor.to_json()` now writes (a 0.1 state file still loads; see
below). `Message` gains fields and properties only (keyword-only, defaulted);
`__all__` only grows.

### Added

- `chats.chats_changed_since(conn, rowid)` / `ChatDB.chats_changed_since(rowid)`
  and the `ChatActivity(chat_rowid, last_rowid)` model: every chat with at
  least one message `ROWID > rowid`, with its newest ROWID, newest activity
  first. Computed from `chat_message_join` alone (`sql.CHATS_CHANGED_SINCE`:
  `SELECT chat_id, MAX(message_id) ... WHERE message_id > ? GROUP BY chat_id`,
  one range scan of the `message_id` index, no `message`/`chat` join), so a
  cached `chats()` list can be kept fresh at a cost proportional to the new
  messages rather than to the database (README section 7). `rowid=0` lists
  every chat that has messages; a cursor at or past `max_rowid()` yields `[]`.
  `chats()` and every 0.1.x call are unchanged.
- Unsend ("retracted") events. `Cursor` gains a third field, `retract_mark`
  (the raw `message.date_retracted` high-water mark, `0` by default);
  `to_json()` now writes it and `from_json()` reads a missing or `None` key as
  `0`, so a state file written by 0.1 still loads (and replays every past
  unsend once). `Event.kind` gains `"retracted"`; within a round `poll_once`
  emits `"new"`, then `"edited"`, then `"retracted"` events (rows whose
  `date_retracted` advanced past the mark, ascending by `date_retracted`),
  with the same per-event cursor rules as edits: a `"retracted"` event's
  cursor carries the round's final ROWID and edit mark plus the running
  retract mark, ties on `date_retracted` advance the mark only on the tie's
  last event, the mark never regresses, and the last event's cursor equals
  the round's. The kind is **opt-in**: `include_retractions` keyword
  (default `False`) on `poll_once`, `watch`, `run_watch`, `ChatDB.poll` and
  `ChatDB.watch`. A consumer written for 0.1 (`if ev.kind == "new": ...
  else: apply_edit(...)`) therefore sees exactly the 0.1 stream; pass
  `include_retractions=True` to receive the third kind. While it is off the
  retract mark does not move, so switching it on later delivers the unsends
  missed meanwhile. `ChatDB.initial_cursor()` seeds `retract_mark` from the
  new `max_date_retracted()` regardless of the flag. On a schema without
  `date_retracted` (macOS before 26) no such events are produced and the mark
  stays `0`.
- `messages.messages_retracted_after(conn, schema, mark)` /
  `ChatDB.messages_retracted_after(mark)` -> `(messages, new_mark)` and
  `messages.max_date_retracted(conn, schema)` / `ChatDB.max_date_retracted()`,
  mirroring the `date_edited` pair; `sql.RETRACTED_AFTER_SUFFIX` and
  `sql.MAX_DATE_RETRACTED` are the relay's edit statements with
  `date_retracted` substituted (pinned as derived statements, not golden).
- An unsent message keeps its row with `date_retracted` set and its text
  typically cleared, so a `"retracted"` event carries the row as it is now;
  callers remove or mark the message by `rowid`/`guid`. The 0.1.0 note
  "retractions are not an event kind" no longer applies.
- A full `attributedBody` typedstream reader, `imessage_chatdb.typedstream_reader`
  (docs/TYPEDSTREAM.md): `parse_attributed_body(data) -> AttributedBody`
  (`text`, the attribute `runs` with UTF-16 offsets and read-only attribute
  mappings, `root_class`/`mutable`) and `try_parse_attributed_body` (never
  raises; `None` for empty or malformed input). Attribute values arrive as
  `str`, `int`, `bytes`, `Url`, nested mappings, `None`, or `UnknownValue`
  for a class the reader does not interpret (its fields are parsed
  structurally and dropped, so the text is never lost to one odd attribute).
  `message_parts(body)` groups the runs by `__kIMMessagePartAttributeName`
  into `AttachmentPart` (a U+FFFC run with a file-transfer guid), `TextPart`
  (text, `Mention`s with code-point offsets, deduplicated link URLs) and
  `UnknownPart`; `AttributeRun.chars(text)` converts UTF-16 offsets to a
  code-point slice (text without astral code points is sliced directly).
  Any defect raises `TypedStreamError` with the byte offset: the exact
  header, trailing bytes, a truncated blob (the text is never partial), bad
  or cyclic references, object nesting deeper than 64, a class chain longer
  than 64, converted attribute values nested more than 64 mappings deep
  (counted through back-references as well as literals), `0x83` in an
  integer context, unknown type characters and non-byte arrays (an array
  count accepts ASCII digits only: a corrupt byte that decodes to a Unicode
  digit such as `²` is a `TypedStreamError`, not a `ValueError` from
  `int()`; found by fuzzing live blobs). Every depth is bounded by counting,
  never by the interpreter's recursion limit: class chains are read
  iteratively and a crafted blob of thousands of chained classes or of
  thousands of dictionaries linked by references raises `TypedStreamError`,
  not `RecursionError` (`try_parse_attributed_body` additionally catches
  `RecursionError` as a backstop, so `Message.body`, `Message.parts` and
  `tail --parts` keep their never-raise promise). Each archived object is
  converted once and the result shared (a 100 KB string referenced by a
  thousand keys is decoded once; a dictionary referenced from another is
  converted once), so parsing is linear in the input in time and memory;
  `message_parts` converts every run's UTF-16 offsets in one pass over the
  text, linear in text plus runs. In value position a *known* class with an
  unexpected layout (an `NSString` without its `+` field, a nested
  `NSDictionary` whose pair count or keys are off, an `NSURL` whose string is
  broken) becomes `UnknownValue` exactly as an unknown class does, so one odd
  attribute never costs the text; the text string, a dictionary key and a
  run dictionary stay strict and raise. `extract_text` / `effective_text`
  are untouched and remain the oracle: on every writer-produced blob
  `parse_attributed_body(b).text == (extract_text(b) or "")`. The 0.1 test
  writer's single-string blobs are *rejected* by the reader (their trailer
  departs from the live bytes in three places, docs/TYPEDSTREAM.md section
  7); the 0.2 writer `encode_attributed_body_runs` is byte-exact.
- `Message.attributed_body_raw` (the raw blob, keyword-only, default `None`;
  `row_to_message` fills it), `Message.body` (`try_parse_attributed_body` of
  it, cached on first access) and `Message.parts` (`message_parts(body)` or
  `()`). `to_dict()`, `==`, `hash()` and `repr()` are unchanged.
- CLI: `tail --parts` and `search --parts` append a `"parts"` key (a list of
  `Part.to_dict()` dicts: `bytes` as `{"base64": ...}`, `Url` as its string,
  `UnknownValue` as `{"class": ...}`); without the flag every line is
  byte-identical to 0.1.1. `search --parts` costs one extra `message()` fetch
  per hit.

### Changed

- Nothing in 0.1.x behaviour. `Cursor.to_json()` emits one more key,
  `retract_mark` (always present, `0` until a `"retracted"` event has been
  taken); `Cursor.from_json()` reads a dict without it as `0`, so state files
  written by 0.1 load unchanged and the default event stream is the 0.1
  stream (see `include_retractions` above).
- Tests: `test_fuzz.py` now feeds `try_parse_attributed_body` the same
  random, bit-flipped and seeded inputs as the other never-raise decoders.

## 0.1.1 — 2026-10-06

### Added

- `search(conn, q, *, chat_guid=None)` / `ChatDB.search(q, *, chat_guid=None)`:
  when `chat_guid` is given only that chat's messages are searched
  (`chat.guid = ?`, bound as a literal), so the oversample, Python recheck and
  `limit` cap apply to that chat's rows alone and hits in other chats never
  consume the budget; a guid with no matching chat yields `[]`. Implemented
  with a second statement, `sql.SEARCH_IN_CHAT`, derived at import time from
  `SEARCH` by inserting one `AND c.guid = ?` line; `SEARCH` itself is
  untouched and still pinned byte-for-byte against the relay, and the
  `chat_guid=None` path runs exactly the SQL it did in 0.1.0.
- CLI: `search TEXT --chat GUID`.

## 0.1.0 — 2026-10-03

First release: the chat.db reading code extracted from a private iMessage
relay into a stdlib-only, read-only, typed library whose results feed that
relay byte-for-byte.

### Added

- `open()` / `ChatDB`: one fresh read-only connection per call (`?mode=ro`
  URI + `PRAGMA query_only = 1`, autocommit, every query `fetchall()`-ed,
  never `immutable=1`); `connection()` context manager for batching;
  `readonly_uri=False` compatibility shim for callers that used a plain
  `sqlite3.connect` (opens `?mode=rw`: read-write at the SQLite level, never
  creates a missing file; SQLite is the first thing to touch the path in both
  modes, there is no `os.path.exists` probe). `open()` and `refresh_schema()`
  check the required
  columns, so a SQLite file that is not a Messages database raises
  `SchemaError` up front. `imessage_chatdb.open` is an attribute but not in
  `__all__` (a star import never shadows the builtin).
- Errors: `ChatDBError`, `ChatDBAccessError` (`"<path>: <sqlite message>.
  <advice>"` in both open modes; the advice names `sys.executable` and the
  Full Disk Access pane, with the Homebrew-upgrade trap; `path`, `reason` and
  `executable` are attributes), `ChatDBBusy`
  (retryable), `CursorAhead(cursor_rowid, max_rowid)`, `SchemaError(column)`.
- `Schema`: `PRAGMA table_info` introspection of the seven tables, required /
  optional column lists, `optional_missing`, best-effort `profile`
  (`macos14`, `macos15`, `macos26`, `macos27`), `build_message_select()` that
  renders missing optional columns as `NULL AS alias` and trims `payload_data`
  to URL-balloon rows.
- Messages: `messages_after`, `messages_edited_after` (raw Apple-int edit
  mark that never regresses), `thread_messages` (newest N, returned
  ascending), `recent_messages`, `message`, `messages_by_guid`, `reply_targets`,
  `enrich` (one attachments query for rows with attachments + one reply-target
  query per batch), `max_rowid`, `max_date_edited`, `include_orphans`.
- Attachments: `attachments_for`, `attachment_by_guid`, `chat_attachments`,
  `payload_for`; the default filter excludes `*.pluginPayloadAttachment` by
  `transfer_name` suffix (not `hide_attachment`, which would also hide real
  images Messages shows).
- Chats: `chats_by_activity`, `chat`, `chat_by_rowid`, `participants`,
  `participants_map`, `chat_services` (outgoing > any > chat precedence),
  `last_rowid_for`, `unread_count`, `lite_messages`, `one_to_one_activity`,
  `find_chat` with a pluggable `key` and `exclude` (one address = one-to-one
  branch; otherwise exact participant-set match in the group branch).
- Search: NUL-safe `instr()` search over `attributedBody` with a Python
  recheck on the decoded text; `extract_urls`, `snippet`.
- Polling (`imessage_chatdb.polling`; `Cursor`, `Event`, `poll_once`,
  `watch`, `run_watch` are all package exports, so `from imessage_chatdb
  import watch` is the generator function and `ChatDB.watch` the same thing):
  frozen `Cursor(rowid, edit_mark)` with JSON round-trip; `Event(kind,
  message, cursor)` where `cursor` is the `Cursor` to persist once that event
  has been handled (`"new"`: its own ROWID with the round's starting mark;
  `"edited"`: the round's final ROWID with the largest `date_edited` so far,
  where rows tied on `date_edited` advance the mark only on the tie's last
  event so a checkpoint inside a tie replays the tie instead of skipping its
  rest; the last event's cursor equals the one `poll_once` returns), so a
  `watch()` consumer that checkpoints after each event resumes with
  at-least-once delivery; `poll_once` (busy -> no events, nothing advanced; `new` before
  `edited`; `CursorAhead` on a rebuilt database), `watch()` generator,
  `run_watch()` with the per-round `on_cursor` persistence hook.
- Decoders (never raise): `extract_text` (typedstream byte-scan),
  `effective_text` (the relay's text rule, exactly), `clean_text`;
  `KeyedArchive` (plistlib + UID dereference with a visited set),
  `parse_link_preview` / `LinkPreview`, `embedded_images` (`None` for an
  unusable payload, `[]` for an archive without an image, else the blobs in
  table order), `embedded_image` (the largest of them), `sniff_image_mime`.
- Pure helpers: `apple_to_unix` (the relay's expression verbatim, `0`/`None`
  -> `None`), `unix_to_apple`, `apple_to_datetime`; `classify_reaction` /
  `Reaction` for tapbacks 2000-2006 / 3000-3006 / 1000 incl. macOS 26+
  `associated_message_emoji`; `parse_associated_guid` for `p:`, `bp:` and
  bare GUID forms; `Service` enum, `normalize_service`, `service_family`;
  `address_key` (documented as North American: the last 10 digits, which
  collides or mismatches on international numbers — pass `find_chat` your own
  `key=`), `is_email`, `is_group_style`.
- Models: frozen slotted dataclasses `Message`, `Attachment`, `ReplyTarget`,
  `LiteMessage`, `Chat`, `ChatSummary`, `ChatMatch`, `SearchHit` with raw
  Apple dates, `*_unix` / `datetime` properties and `to_dict()` in the relay's
  key order with raw values; `ReplyTarget.to_dict()` (the `reply_to` of
  `Message.to_dict()`) is facts only — `{"guid", "text", "is_from_me",
  "sender_handle"}`, untruncated, no `"You"` / `"Attachment"` strings (those
  are built by the relay's adapter).
- CLI: `python -m imessage_chatdb check | tail [--since-rowid N] [--follow]
  [--limit N] [--interval S] | chats [--limit N] | search TEXT [--limit N]`,
  `--db PATH` on every subcommand, JSON-lines output; exit codes 0 ok, 1
  other library error, 2 with the access explanation on `ChatDBAccessError`,
  64 (`EX_USAGE`) for a command-line usage error so that 2 stays an
  unambiguous "Full Disk Access lost" signal, 130 interrupted; a stdout closed
  early by its reader (`tail ... | head -1`) ends the command quietly with
  exit 0 (the broken descriptor is pointed at `os.devnull` so the interpreter's
  shutdown flush cannot fail again); `main(argv)` returns the code and never
  raises `SystemExit`. `tail` and `search` print message content to stdout
  as JSON lines (the only place the library does), which `--help`, the module
  docstring and the README say not to point at a log or a remote pipe.
- Tests: four schema profiles built under `tmp_path` only (the suite refuses
  `~/Library`); `attributedBody` and `payload_data` writers that reproduce the
  live byte layouts; WAL-vs-`immutable` and lock-contention tests; 2,000-
  iteration seeded fuzzing of every decoder with random bytes, bit-flipped
  valid blobs and random-UID plists; README snippets executed against a
  fixture.
- Packaging: `DESIGN.md` (the design notes, kept in the repository) is
  excluded from the sdist; `README.md`, `LICENSE` and `py.typed` ship.

### Known deviations from the relay

- **Typedstream length tag `0x82` is read as 4 bytes** (u32 little-endian);
  the relay read 3. `0x83` is read as 8 bytes; any other tag `>= 0x80` yields
  `None` instead of being used as a raw length. This is the spec-correct
  reading. No live row uses `0x82` or `0x83` (the longest observed text is
  under 10 KB and `0x81` + u16 covers up to 65,535), so the relay's output
  cannot change today; the case is exercised synthetically.
- **A truncated `attributedBody` decodes to `None`, not a partial string.**
  The rule, pinned by the truncation-prefix tests at every byte offset: a blob
  cut before the end of its text — in the header, in the length tag or its
  length bytes, or inside the text itself — makes `extract_text` return
  `None`, never a partial string; a blob cut only in the `0x86` trailer after
  the text (the text is complete) decodes to the complete text, exactly as an
  intact blob would. The relay returned whatever bytes were there. Every live
  blob carries the trailer after its text, so the `None` case is reachable
  only on a corrupt blob; through `effective_text` such a row's `text` is
  `None` where the relay gave a fragment.

### Deliberately preserved relay behaviour (to be revisited as versioned changes)

- `iMessageLite` is folded into the `iMessage` family by `service_family`.
- Attachment and participant queries carry no `ORDER BY` (SQLite scan order).
- Messages without a `chat_message_join` row are hidden unless
  `include_orphans=True`.
- `date_retracted` is exposed as a field; retractions are not an event kind.
- The `guid.startswith("iMessage")` tie-break in `find_chat` is kept even
  though every live chat guid starts with `any;`.

### Not in this release

- Full typedstream parts reader, edit history from `message_summary_info`,
  retraction events, `date_updated` as a universal change mark, FTS.
