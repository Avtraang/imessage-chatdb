# Changelog

All notable changes to `imessage-chatdb` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

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
