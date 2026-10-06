# imessage-chatdb

[![CI](https://github.com/Avtraang/imessage-chatdb/actions/workflows/ci.yml/badge.svg)](https://github.com/Avtraang/imessage-chatdb/actions/workflows/ci.yml)

Read-only, standard-library-only reader for Apple Messages' `chat.db`
(`~/Library/Messages/chat.db`). Open the database safely, tail new messages
from a ROWID cursor, detect edits and unsends, decode `attributedBody`, classify tapbacks,
follow replies, read link previews, list attachments and chats, and search —
in a few dozen lines, with no dependencies.

Project home: <https://github.com/Avtraang/imessage-chatdb> · Python >= 3.12 · MIT

## 1. What it is, what it is not

**It is**

- a read-only reader: every connection is opened with `?mode=ro` *and*
  `PRAGMA query_only = 1`; nothing here can write to `chat.db`;
- stdlib only at runtime (`sqlite3`, `plistlib`, `dataclasses`), typed
  (`py.typed`), frozen slotted dataclasses, no logging of message content;
- schema tolerant: columns are read by name, optional columns missing on an
  older macOS come back as `None`, and nothing breaks when Apple reorders a
  table;
- a library of *facts*: raw handles, raw Apple dates, raw service strings. The
  strings your app shows ("You", first names, "Photo") are yours to make.

**It is not**

- a sender. It never writes, never drives Messages.app, never uses AppleScript.
- a contacts resolver. Handles are the raw `handle.id` (`+1555…`, an email).
- a server. Attachment URLs, thumbnails, HEIC conversion, read state and pins
  belong to whatever you build on top.
- cross-platform. Everything but the default path works anywhere SQLite does
  (the test-suite runs on Linux), but the database only exists on a Mac.

## 2. Install and Full Disk Access (30 seconds)

The only system requirement is a one-time Full Disk Access grant for your Python. System Integrity Protection (SIP) stays on: nothing is injected into Messages and no private API is used. Reading a file is all this library does.

```sh
pip install imessage-chatdb
python -m imessage_chatdb check
```

`check` opens the database and prints one JSON line with the detected schema
profile, the highest ROWID and `"Full Disk Access OK"`:

```text
{"ok": true, "path": "~/Library/Messages/chat.db", "profile": "macos27", "max_rowid": 12345, "optional_missing": [], "message": "Full Disk Access OK"}
```

If instead you see `unable to open database file` or `authorization denied`,
macOS is blocking the read. Grant **Full Disk Access** to the exact interpreter
binary the error names (`sys.executable`): System Settings > Privacy & Security >
Full Disk Access > `+` > pick that binary (press ⌘⇧G and paste the path).

Two traps:

- **The grant is per binary, not per language.** A venv's `python` is a
  symlink; macOS follows it to the real interpreter, so grant the real one.
- **Upgrading the interpreter orphans the grant.** `brew upgrade python`
  installs a new binary at a new path and the old grant silently stops
  applying. Re-run `check` and re-add the new path.

`check` exits 2 and prints the full explanation (the path it tried, then the
binary path) on stderr whenever this happens, so it is a fine health probe for a LaunchAgent.
Exit code 2 means exactly that; a mistyped command or flag exits 64 instead,
so a probe keyed on 2 cannot misfire on a typo.

### The command line

```text
python -m imessage_chatdb check                    # profile, max ROWID, FDA status
python -m imessage_chatdb tail --since-rowid 20000  # Message.to_dict() JSON lines, ascending
python -m imessage_chatdb tail --follow             # start at now, stream new messages
python -m imessage_chatdb chats --limit 20         # ChatSummary.to_dict(), newest activity first
python -m imessage_chatdb search "dinner" --limit 10  # SearchHit.to_dict(), newest first
```

Every subcommand takes `--db PATH` (default `~/Library/Messages/chat.db`).
`tail` without `--since-rowid` starts at *now*, like `watch()`: it prints
nothing unless `--follow` is given. **Privacy:** `tail`, `chats` and `search`
print message content (text, handles, chat names) to stdout as JSON lines, so
do not point them at a log file or a remote pipe — the library itself never
logs or prints message content, and `check` prints only the path, profile and
max ROWID. Exit codes: 0 ok (also `--help`), 2
cannot open the database (message on stderr), 1 any other library error
(busy, schema), 64 command-line usage error (unknown subcommand, bad flag or
value; usage on stderr), 130 interrupted. `main(argv)` returns these codes
and never raises `SystemExit`. A stdout closed early by its reader —
`python -m imessage_chatdb tail --since-rowid 0 | head -1` — is not an error:
output stops, nothing is printed about the broken pipe, and the exit code is 0.

## 3. Quickstart

Every runnable snippet below uses a `DB_PATH` variable. Set
`DB_PATH = "~/Library/Messages/chat.db"` — or call `open()` with no argument,
which is the same thing. (Blocks whose first line is `# doctest-fixture` are
executed by the test-suite against a synthetic database; see section 10.)

### Open, list chats, read the last 20 messages of one chat

```python
# doctest-fixture
import imessage_chatdb

db = imessage_chatdb.open(DB_PATH)               # raises ChatDBAccessError if FDA is missing
print(db.schema.profile, db.max_rowid())

for chat in db.chats(limit=5):                   # newest activity first
    print(chat.chat_name, "group" if chat.is_group else "1:1", chat.last_rowid)

newest = db.chats(limit=1)[0]
for m in db.thread_messages(newest.guid, limit=20):   # oldest -> newest
    who = "me" if m.is_from_me else (m.sender_handle or "?")
    print(m.datetime, who, m.clean_text, [a.transfer_name for a in m.attachments])
```

### Tail new messages with `watch()`, persisting the cursor

```python
import json
from pathlib import Path
import imessage_chatdb
from imessage_chatdb import Cursor

STATE = Path("~/.imessage-cursor.json").expanduser()
db = imessage_chatdb.open()
if STATE.exists():
    cursor = Cursor.from_json(json.loads(STATE.read_text()))   # resume where the last run stopped
else:
    cursor = db.initial_cursor()                               # first run: start at now, no replay

for event in db.watch(cursor, interval=2.0):     # blocks; Ctrl-C to stop
    m = event.message
    print(event.kind, m.rowid, m.chat_guid, m.clean_text)   # kind: "new" | "edited"
    STATE.write_text(json.dumps(event.cursor.to_json()))    # persist AFTER handling the event
```

Every `Event` carries `event.cursor`, the `Cursor` to persist once *that* event
has been handled; resuming from it re-delivers nothing before the event and
everything after it. Persisting after handling gives **at-least-once**
delivery: a crash between handling an event and writing its cursor re-delivers
that one event on restart, and nothing between the last checkpoint and the
crash is lost — so make your consumer idempotent on `(kind, rowid)`. The one
place "nothing before" is relaxed is a tie: `"edited"` rows that share the
same raw `date_edited` form a group, only the group's last event advances
`edit_mark` past that value, and a checkpoint taken on an earlier member
replays the whole group on restart (the handled members again, never the
unhandled ones skipped).

`watch(None)` starts at *now* (no replay), opens a fresh connection per round,
yields `"new"` events before `"edited"` events within a round (and
`"retracted"` events after both, once you opt in with
`include_retractions=True`; see "Unsends" below), and simply yields nothing in
a round where the database is busy. The polling code lives
in `imessage_chatdb.polling`; `watch`, `run_watch`, `poll_once`, `Cursor` and
`Event` are all re-exported, so `from imessage_chatdb import watch` is the
generator function and `db.watch(...)` is the same thing.

### Poll yourself and persist the cursor

`poll()` is the primitive `watch()` is built on; it is what to call from a
scheduler, an asyncio loop or a test.

```python
# doctest-fixture
import json
import imessage_chatdb
from imessage_chatdb import Cursor

db = imessage_chatdb.open(DB_PATH)

cursor = db.initial_cursor()                     # Cursor(rowid=MAX(ROWID), edit_mark=MAX(date_edited),
                                                 #        retract_mark=MAX(date_retracted))
events, cursor = db.poll(cursor)                 # one round; [] when nothing changed
for ev in events:
    print(ev.kind, ev.message.rowid, ev.message.clean_text)

state = json.dumps(cursor.to_json())             # {"rowid": ..., "edit_mark": ..., "retract_mark": ...}
cursor = Cursor.from_json(json.loads(state))     # next run picks up where this one stopped
print(cursor)
```

## 4. Concepts

### The ROWID cursor (at-least-once)

New messages get increasing `message.ROWID`s. A `Cursor` remembers the last
ROWID delivered and advances only to the last message actually returned, so a
crash between fetch and persist re-delivers rather than skips. Each `Event`
carries the cursor to persist after handling it (`event.cursor`); within a
round, a `"new"` event's cursor is its own ROWID with the round's starting
`edit_mark`, an `"edited"` event's cursor is the round's final ROWID with the
largest `date_edited` seen so far (not counting its own value while a later
event in the round still shares it — ties replay as a group), and the last
event's cursor is the one `poll()` returns. Make your consumer idempotent on
`rowid`.

### Edits via `date_edited`

An edit changes a row in place — the ROWID cursor cannot see it. The cursor's
second field, `edit_mark`, is the highest raw `date_edited` seen; `poll()`
returns `"edited"` events for rows past it and never lets the mark regress.
On a macOS without the column, no edit events are produced (and `edit_mark`
stays 0).

### Unsends via `date_retracted`

An unsend ("Undo Send") does not delete the row: Messages.app keeps it, sets
`date_retracted` and, by all reports, clears its text. The cursor's third
field, `retract_mark`, is the highest raw `date_retracted` seen; with
`include_retractions=True` on `poll()` / `watch()` / `run_watch()`, a round
returns `"retracted"` events for rows past it, after the round's `"edited"`
events, and never lets the mark regress. **The kind is off by default**, so a
consumer that only knows `"new"` and `"edited"` keeps seeing exactly that
stream (0.2 is additive); while it is off the mark does not move, and
switching it on later delivers the unsends missed meanwhile. The event carries
the row *as it is now* — `message.text` is typically `None` — so handle it by
`rowid` or `guid`: remove or mark the message you delivered earlier; do not
expect its content. Ties on `date_retracted` replay as a group exactly like
edit ties. The column exists on macOS 26+; on older databases no `"retracted"`
events are produced and `retract_mark` stays 0. A cursor saved by 0.1 has no
`retract_mark` key and loads as 0, which replays every past unsend once — take
`db.initial_cursor()` instead if you would rather skip them.

```python
# doctest-fixture
import imessage_chatdb
from imessage_chatdb import Cursor

db = imessage_chatdb.open(DB_PATH)
cursor = Cursor(rowid=db.max_rowid(), edit_mark=db.max_date_edited())  # retract_mark=0: replay unsends
events, cursor = db.poll(cursor, include_retractions=True)
for ev in events:
    if ev.kind == "retracted":                   # the row as it is now; text is usually None
        print("unsent", ev.message.rowid, ev.message.guid, ev.message.text)
print(cursor.retract_mark)                       # the mark to persist; 0 on macOS < 26
```

### Apple dates and the `0` sentinel

`message.date*` are nanoseconds since 2001-01-01 (older databases stored
seconds; both are handled). `0` means *never* (`date_edited` is `0`, not
NULL, on unedited rows). The dataclasses keep the raw integers; `*_unix`
and `datetime` properties convert, and `apple_to_unix(0)` is `None`.

```python
# doctest-fixture
from imessage_chatdb import apple_to_unix, unix_to_apple, apple_to_datetime

print(apple_to_unix(0), apple_to_unix(None))             # None None
raw = unix_to_apple(1704067200.0)                        # 2024-01-01T00:00:00Z
print(raw, apple_to_unix(raw), apple_to_datetime(raw))
```

### `text` vs `attributedBody`

Most modern rows have `text IS NULL` and carry the string inside
`attributedBody`, a typedstream archive. `Message.text` is the *effective*
text: the `text` column when non-empty, else the string extracted from the
blob. `Message.text_column` keeps the raw column and `Message.clean_text`
strips U+FFFC attachment placeholders and collapses whitespace. The extractor
never raises; undecodable blobs give `None`.

### Attribute runs, mentions, links and attachment parts (0.2)

`Message.body` parses the whole archive with the typedstream reader
(`imessage_chatdb.typedstream_reader`, specified in `docs/TYPEDSTREAM.md`):
the text plus every attribute run with its UTF-16 offsets and attribute
dictionary. `Message.parts` groups the runs into `AttachmentPart` (a U+FFFC
placeholder carrying its attachment guid), `TextPart` (text with `Mention`
spans and link URLs) and `UnknownPart`. Both are computed on first access,
cached, and never raise: a blob the reader rejects gives `body is None` and
`parts == ()` while `text` is still the byte-scan result. `to_dict()` is
unchanged; the CLI adds the same view with `tail --parts` / `search --parts`.

```python
# doctest-fixture
import imessage_chatdb
from imessage_chatdb import AttachmentPart, TextPart, parse_attributed_body

db = imessage_chatdb.open(DB_PATH)
for m in db.messages_after(0):
    for part in m.parts:                         # () when the row has no parsable blob
        if isinstance(part, AttachmentPart):
            print("attachment", part.guid)
        elif isinstance(part, TextPart):
            print("text", part.text, [x.handle for x in part.mentions], part.links)

blob = next(m.attributed_body_raw for m in db.messages_after(0) if m.body is not None)
body = parse_attributed_body(blob)               # raises TypedStreamError on a malformed blob
for run in body.runs:                            # offsets are UTF-16 code units; chars() gives code points
    print(run.start, run.end, run.chars(body.text), dict(run.attributes))
```

The reader is hardened against crafted blobs: every depth (object nesting,
class-chain length, nesting of converted values through back-references) is
bounded by counting rather than by the interpreter's recursion limit, each
archived object is converted once however many runs reference it, and
`message_parts` is linear in the text plus the runs. A known attribute class
with an unexpected layout becomes `UnknownValue` rather than costing the text.

### Tapbacks (including macOS 26+ emoji)

```python
# doctest-fixture
import imessage_chatdb

db = imessage_chatdb.open(DB_PATH)
for m in db.messages_after(0):
    r = m.reaction                                # Reaction | None
    if r is not None:
        print(r.action, r.kind, r.emoji, "on", r.target_guid, "part", r.target_part)
```

`associated_message_type` 2000-2006 add and 3000-3006 remove a tapback
(`love, like, dislike, laugh, emphasize, question, emoji`); 1000 is a sticker.
Type 2006/3006 carries the emoji in `associated_message_emoji`. The target is
parsed from `associated_message_guid`: `p:<part>/<GUID>`, `bp:<GUID>`
(balloon) or a bare GUID. `Message.is_tapback` is the quick filter.

### Replies

`Message.reply_to_guid` is `thread_originator_guid`; after enrichment
(the default) `Message.reply_to` is a `ReplyTarget` with the quoted message's
cleaned, untruncated text (`''` for an attachment-only target), `is_from_me`
and `sender_handle`. `ReplyTarget.to_dict()` — the `reply_to` of
`Message.to_dict()` — emits exactly those facts, `{"guid", "text",
"is_from_me", "sender_handle"}`, never a `"You"`, an `"Attachment"`
placeholder or a truncated preview; those strings are yours.

```python
# doctest-fixture
import imessage_chatdb

db = imessage_chatdb.open(DB_PATH)
for m in db.messages_after(0):
    if m.reply_to is not None:
        print(m.clean_text, "-> replying to:", m.reply_to.text or "(attachment)")
```

### Link previews

A URL balloon (`balloon_bundle_id == LINK_BALLOON`) stores an
`NSKeyedArchiver` plist in `payload_data`. `Message.link` is a `LinkPreview`
(`url, title, summary, site, image_url, has_embedded_image`); when the card's
image is embedded in the archive, `db.link_image(rowid)` returns the bytes
and `sniff_image_mime` names the type. `link_image` returns `None` for a
missing row, an empty payload, an unparseable payload and an archive without
an image alike; an HTTP layer that wants a different status body for each uses
`db.payload(rowid)` plus `embedded_images(payload)`, which returns `None` for
an unusable payload and `[]` for a well-formed archive with no image.

```python
# doctest-fixture
import imessage_chatdb
from imessage_chatdb import sniff_image_mime

db = imessage_chatdb.open(DB_PATH)
for m in db.messages_after(0):
    if m.link is not None:
        print(m.link.title, m.link.url, m.link.image_url)
        if m.link.has_embedded_image:
            blob = db.link_image(m.rowid)
            print(sniff_image_mime(blob[:12]), len(blob), "bytes")
```

### Attachments and the plugin-payload filter

```python
# doctest-fixture
import imessage_chatdb

db = imessage_chatdb.open(DB_PATH)
for chat in db.chats(limit=5):
    for a in db.chat_attachments(chat.guid):      # newest first, plugin payloads excluded
        print(chat.chat_name, a.transfer_name, a.mime_type, a.path, a.exists())
    everything = db.chat_attachments(chat.guid, include_plugin_payloads=True)
    print(chat.chat_name, len(everything), "incl. plugin payloads")
```

Every rich-link card also writes an `*.pluginPayloadAttachment` row. Those are
excluded by default (`include_plugin_payloads=False`). The filter is the
`transfer_name` suffix, deliberately **not** `hide_attachment`: that flag also
hides a handful of real images that Messages still shows. `Attachment.path`
expands `~`; `exists()` tells you whether iCloud has actually downloaded it.

### Services, including iMessageLite

`Message.service_raw` is the raw `message.service` (`iMessage`, `SMS`, `RCS`,
`iMessageLite`); `Message.service` is a `Service` enum (`UNKNOWN` for anything
else). `service_family()` folds `iMessageLite` into `iMessage` the way
Messages' UI does. `chat.service_name` is only a stale hint; prefer the
message's own service or `db.chat_services(rowids)`.

### Group vs one-to-one

`chat.style` 43 is a group, 45 is one-to-one (`Chat.is_group`,
`Message.is_group`, `is_group_style`). Participants are `db.participants(rowid)`
(raw handles, from `chat_handle_join`) or `db.participants_map()` for all
chats in one query.

### Matching a chat by participants, with your own `key`

> **Warning — the default `address_key` is North American.** It keeps the
> **last 10 digits** of a phone number (the NANP national number), which
> collides for international numbers that share their last ten digits
> (`+44 20 7946 0958` and `+33 20 7946 0958` both become `2079460958`) and
> mismatches a number whose national part is shorter than ten digits when it is
> stored once with and once without its country code. Outside the NANP pass
> your own `key=`; the one-liner that compares full digit strings is
> `db.find_chat(addrs, key=lambda a: address_key(a) if "@" in a else "".join(ch for ch in a if ch.isdigit()))`.

```python
# doctest-fixture
import imessage_chatdb
from imessage_chatdb import address_key

db = imessage_chatdb.open(DB_PATH)
match = db.find_chat(["+1 (555) 000-1234"])      # digits only, last 10 -> "5550001234" (NANP!)
print(match.chat_guid if match else None)

ALIASES = {"test@example.invalid": "5550001234"}  # your own phone<->email merge

def my_key(addr: str) -> str | None:
    return ALIASES.get(addr.strip().lower()) or address_key(addr)

print(db.find_chat(["test@example.invalid"], key=my_key, exclude=["+15550009999"]))
```

One address takes the one-to-one branch; two or more take the group branch,
which requires the exact participant set (joins plus any incoming senders,
minus `exclude` — your own handles). The newest chat wins a tie.

### Search (NUL-safe)

```python
# doctest-fixture
import imessage_chatdb
from imessage_chatdb import snippet

db = imessage_chatdb.open(DB_PATH)
for hit in db.search("hello", limit=10):          # newest first
    print(hit.rowid, hit.chat_name, snippet(hit.text, hit.match_index))
```

`LIKE` stops at the first NUL byte of a typedstream blob, so `attributedBody`
is searched with `instr()` on the raw bytes (four case variants) and every SQL
hit is re-checked in Python against the decoded text. It is a full scan: keep
`limit` modest on large databases (section 7).
`db.search("hello", chat_guid="any;-;+15550001234")` (CLI: `search TEXT --chat GUID`) scopes the scan to one chat, so hits in other chats never consume the oversample budget.

### Schema tolerance and profiles

`db.schema` (a `Schema`) is `PRAGMA table_info` on the seven tables the library
reads. Required columns are checked (`SchemaError("message.guid")`) when the
schema is introspected — by `open()`, by the first `ChatDB` call that needs it,
and by `refresh_schema()` — so pointing the library at a SQLite file that is not
a Messages database fails with the library's own `SchemaError` rather than a
raw `sqlite3.OperationalError`. Optional columns (`date_edited`, `service`,
`associated_message_emoji`, `payload_data`, `thread_originator_part`, `is_spam`,
…) become `None` when missing and `Schema.optional_missing` lists them.
`Schema.profile` is a best-effort label (`macos14` … `macos27`, section 9).

### Orphans

A few hundred rows in a real database have no `chat_message_join` row. They
are hidden by default (the inner join the relay has always used);
`messages_after(..., include_orphans=True)` returns them with `None` chat
fields.

### `CursorAhead`

If `MAX(ROWID)` drops below your persisted cursor the database was rebuilt
(iCloud re-sync, a restore). `poll()` and `watch()` raise `CursorAhead`
rather than guess; catch it and restart from `db.initial_cursor()` or from
`Cursor()` to replay everything.

### Errors

```python
# doctest-fixture
import imessage_chatdb
from imessage_chatdb import ChatDBAccessError

try:
    imessage_chatdb.open("/nonexistent/chat.db")
except ChatDBAccessError as e:
    print(e)      # "<path>: <sqlite message>. <Full Disk Access advice naming sys.executable>"
```

`ChatDBBusy` (retryable lock), `CursorAhead`, `SchemaError` and
`ChatDBAccessError` all derive from `ChatDBError`. `ChatDBAccessError` has the
same shape in both open modes and carries `path`, `reason` (the bare SQLite
message) and `executable` as attributes. Decoders never raise.

## 5. Recipes

### A bot: react to incoming, non-tapback messages

```python
import imessage_chatdb
from imessage_chatdb import CursorAhead

db = imessage_chatdb.open()
cursor = load_cursor() or db.initial_cursor()    # your persistence; None on first run
while True:
    try:
        for event in db.watch(cursor, interval=2.0):
            m = event.message
            if event.kind == "new" and not (m.is_from_me or m.is_tapback or not m.clean_text):
                handle_incoming(m.chat_guid, m.sender_handle, m.clean_text)   # your code
            cursor = event.cursor
            save_cursor(cursor)               # after handling: at-least-once on restart
    except CursorAhead:
        cursor = db.initial_cursor()          # the database was rebuilt; start over at now
```

### An exporter: copy every attachment of one chat

```python
# doctest-fixture
import shutil
import tempfile
from pathlib import Path
import imessage_chatdb

db = imessage_chatdb.open(DB_PATH)
chat = db.find_chat(["+15550001234"])
out = Path(tempfile.mkdtemp())
if chat is not None:
    for a in db.chat_attachments(chat.chat_guid):
        if a.path is not None and a.exists():
            shutil.copy2(a.path, out / f"{a.rowid}-{a.transfer_name or a.path.name}")
print(sorted(p.name for p in out.iterdir()))
```

### A bridge: asyncio + `to_thread(db.poll)`

Connections are never shared across threads, and `poll()` opens and closes its
own, so handing it to a worker thread is the whole async story.

```python
import asyncio
import imessage_chatdb

async def main() -> None:
    db = imessage_chatdb.open()
    cursor = db.initial_cursor()
    while True:
        events, cursor = await asyncio.to_thread(db.poll, cursor)
        for ev in events:
            await forward(ev.message.to_dict())   # your async sender
        await asyncio.sleep(2.0)

asyncio.run(main())
```

`Message.to_dict()` is JSON-ready: the relay's key names in the relay's order
with raw values — no contact names, no attachment URLs, dates as Unix floats.

## 6. Data model reference

All classes are `@dataclass(frozen=True, slots=True)`. Dates are raw Apple
integers (`0` = never); `*_unix` properties convert.

**`Message`** (`db.messages_after`, `thread_messages`, `message`, …)

| field | meaning |
|---|---|
| `rowid`, `guid` | `message.ROWID`, `message.guid` |
| `text` | effective text (column, else decoded `attributedBody`); `None` if none |
| `text_column` | raw `message.text` |
| `date`, `date_read`, `date_delivered`, `date_edited`, `date_retracted` | raw Apple ints; optional ones `None` when the column is absent |
| `is_from_me` | `bool` |
| `handle_rowid`, `sender_handle` | `handle.ROWID`, raw `handle.id` (`None` for outgoing) |
| `chat_rowid`, `chat_guid`, `chat_identifier`, `chat_display_name`, `chat_style` | the chat the row is joined to; `chat_rowid` and `chat_guid` are typed optional and are `None` only on `include_orphans=True` rows |
| `service_raw` | raw `message.service` |
| `has_attachments`, `attachments` | `bool`; `tuple[Attachment, ...]` (filled by enrichment, plugin payloads excluded) |
| `associated_guid`, `associated_type`, `associated_emoji` | tapback raw fields |
| `reply_to_guid`, `reply_to_part`, `reply_to` | `thread_originator_*`; `ReplyTarget` after enrichment |
| `balloon_bundle_id`, `link` | balloon id; `LinkPreview` only for the URL balloon |
| `item_type`, `group_action_type`, `group_title`, `filter_action`, `is_spam`, `expressive_send_style_id` | raw, optional |
| `enriched` | whether `attachments`/`reply_to` were resolved |
| properties | `is_group`, `service`, `reaction`, `is_tapback`, `date_unix`, `date_read_unix`, `date_delivered_unix`, `date_edited_unix`, `date_retracted_unix`, `datetime`, `clean_text`, `chat_name`, `to_dict()` |

**`Attachment`** — `message_rowid, rowid, guid, mime_type, transfer_name,
filename, uti, total_bytes, is_sticker, hide_attachment, message_date`;
properties `is_plugin_payload`, `path`, `message_date_unix`; `exists()`;
`to_dict()` -> `{"guid","mime_type","name","filename"}`.

**`ReplyTarget`** — `guid, text (cleaned, untruncated, '' if none), is_from_me,
sender_handle`; `to_dict()` -> `{"guid", "text", "is_from_me", "sender_handle"}`
(facts only: no `"You"`, no `"Attachment"`, no truncation).

**`LiteMessage`** (`db.lite_messages`) — `rowid, text, is_from_me,
associated_type, associated_emoji, has_attachments, sender_handle, attachments`:
the facts behind a chat list's "last message" line.

**`Chat`** — `rowid, guid, style, chat_identifier, display_name, service_name,
is_archived, is_filtered, group_id`; properties `is_group`, `chat_name`.
**`ChatSummary(Chat)`** adds `last_date`, `last_rowid` (`db.chats()`).

**`ChatMatch`** (`db.find_chat`) — `chat_rowid, chat_guid, chat_identifier,
display_name, is_group, last_rowid`.

**`ChatActivity`** (`db.chats_changed_since`) — `chat_rowid, last_rowid`: a
chat that gained messages past a ROWID cursor and its newest ROWID (section 7).

**`SearchHit`** (`db.search`) — `rowid, chat_rowid, chat_guid, chat_identifier,
chat_display_name, is_group, date, is_from_me, sender_handle, text, match_index`.

**`LinkPreview`** — `url, title, summary, site, image_url, has_embedded_image`.

**`Reaction`** — `action ("add"|"remove"), kind, emoji, raw_type, target_guid,
target_part, is_balloon`.

**`Cursor`** — `rowid, edit_mark, retract_mark`; `to_json()` /
`Cursor.from_json()` (a missing `retract_mark` reads as 0).
**`Event`** — `kind ("new"|"edited"|"retracted")`, `message`, `cursor` (the
`Cursor` to persist once this event has been handled). All from `imessage_chatdb.polling`,
re-exported with `poll_once`, `watch` and `run_watch`.

Pure helpers: `apple_to_unix`, `unix_to_apple`, `apple_to_datetime`,
`extract_text`, `effective_text`, `clean_text`, `parse_link_preview`,
`embedded_images`, `embedded_image`, `sniff_image_mime`, `classify_reaction`,
`parse_associated_guid`, `normalize_service`, `service_family`, `address_key`,
`is_email`, `is_group_style`, `extract_urls`, `snippet`, `KeyedArchive`.

Lower level: every `ChatDB` method is a thin wrapper over a module function
that takes an open `sqlite3.Connection` (`imessage_chatdb.messages`,
`.attachments`, `.chats`, `.search`); use `with db.connection() as conn:` to run
several of them on one connection.

## 7. Performance notes and limits

- `ChatDB` opens one connection per call (~0.3 ms) and closes it in
  `finally`. Use `db.connection()` to batch.
- `messages_after` / `thread_messages` are index walks on ROWID; enrichment
  adds exactly one attachments query (only for rows with attachments) and one
  reply-target query per batch.
- `chats()` is a `GROUP BY` over `chat_message_join` — O(messages). Fine at
  20k rows; at 500k+ cache it and keep the cache fresh with
  `chats_changed_since(rowid)` instead of calling `chats()` again (below).
- `search()` is a full scan with a `LIKE` and four `instr()` passes over the
  blobs. Keep `limit` modest and scope by chat where you can; an FTS index is
  impossible on a read-only database.
- Nothing is cached across calls except the `Schema`; call
  `db.refresh_schema()` after a macOS upgrade.

### Keeping a cached chat list fresh

`chats_changed_since(rowid)` reads `chat_message_join` alone
(`SELECT chat_id, MAX(message_id) ... WHERE message_id > ? GROUP BY chat_id`:
one range scan of the `message_id` index, no `message` or `chat` join), so its
cost is proportional to the messages *since your cursor*, not to the database.
Each `ChatActivity(chat_rowid, last_rowid)` names a chat that gained messages
and its newest ROWID — the same `last_rowid` a `ChatSummary` carries — so you
re-sort the cached list by it, and only a `chat_rowid` you have never seen
needs one `chat_by_rowid()` lookup:

```python
# doctest-fixture
import imessage_chatdb

db = imessage_chatdb.open(DB_PATH)
with db.connection():
    cached = {c.rowid: c for c in db.chats()}        # the one full GROUP BY
    cursor = db.max_rowid()

# ... later, after new messages arrived (or on every poll round) ...
for activity in db.chats_changed_since(cursor):      # newest activity first
    known = cached.get(activity.chat_rowid)
    if known is None:                                # a brand-new chat: one row lookup
        print("new chat", db.chat_by_rowid(activity.chat_rowid))
    else:
        print("bump", known.chat_name, known.last_rowid, "->", activity.last_rowid)
    cursor = max(cursor, activity.last_rowid)
print("cursor", cursor)
```

It only reports *new* messages (ROWID order). An edit, a tapback on an old
message or an unsend creates no new ROWID in that chat, so a chat list that
shows previews still watches `poll()` for those; and a chat whose join rows are
all older than `rowid` is simply absent from the result.

## 8. Safety

- **Never `immutable=1`.** It makes SQLite ignore the WAL, so fresh messages
  are invisible until Messages.app checkpoints. The library uses `?mode=ro`
  (which needs the `-shm` file Messages keeps present) plus `PRAGMA query_only`.
- **Every query is `fetchall()`-ed** and connections are autocommit
  (`isolation_level=None`), so no read snapshot lingers in the WAL and
  Messages.app is never blocked from checkpointing.
- **Threads:** connections are never shared across threads; `ChatDB` itself is
  safe to share because it holds no connection. `asyncio.to_thread(db.poll, c)`
  is the pattern.
- `readonly_uri=False` (`ChatDB(..., readonly_uri=False)`) exists only as a
  compatibility shim for callers that historically used a plain
  `sqlite3.connect`; SQLite may then create `-wal`/`-shm` files. Not for new code.
- The library never logs or prints message content, handles or names.

## 9. Compatibility matrix and known deviations

| profile | macOS | notable columns |
|---|---|---|
| `macos14` | 14 | no `associated_message_emoji`, `filter_action`, `date_retracted`, `date_updated`, `thread_originator_part`, `chat.is_filtered` |
| `macos15` | 15 | + `thread_originator_part` |
| `macos26` | 26 | + `associated_message_emoji`, `filter_action`, `date_retracted`, `chat.is_filtered` |
| `macos27` | 27 | + `date_updated`, `is_spam`, `group_title`; table reordered, `sr_*` dropped |

The whole test-suite runs on all four profiles. Detection is best effort —
`Schema.optional_missing` is the truth.

Known deviations from the relay this library was extracted from (both in
`CHANGELOG.md`): the typedstream length tag `0x82` is read as 4 bytes (the
relay read 3) and `0x83` as 8; unknown tags yield `None` instead of a bogus
length. No real row uses either tag (the longest observed text is under 10 KB),
so output is unchanged today. And a *truncated* `attributedBody` — one cut in
its header, in its length tag, or inside the text itself, so the declared text
length runs past the end of the blob — decodes to `None` rather than the
relay's partial string, while a blob cut only in the trailer *after* the text
still decodes to the complete text; every real blob carries its trailer, so
this only matters for corrupt rows.

Not yet: edit history from `message_summary_info`, FTS. (The typedstream
parts reader and unsend events arrived in 0.2.0; see "Attribute runs, mentions,
links and attachment parts" and "Unsends via `date_retracted`" above.)

## 10. Contributing

- Tests use **synthetic databases only**, built under `tmp_path` by
  `tests/fixtures/builders.py`; `conftest.py` refuses any path under
  `~/Library`. Never commit a real database, a `-wal`/`-shm` file or anything
  containing real messages, handles or names (`.gitignore` covers the usual
  suspects). Synthetic handles look like `+15550001234` and `test@example.invalid`.
- `attributedBody` and `payload_data` blobs are fabricated by
  `tests/fixtures/typedstream_writer.py` and `keyed_archive_writer.py`; the
  fuzz tests (`tests/test_fuzz.py`) bit-flip them.
- Code blocks in this README whose first line is `# doctest-fixture` are
  executed by `tests/test_readme_snippets.py` against a synthetic database with
  `DB_PATH` bound; every Python block is at least compiled. Keep them honest.
- `ruff check`, `mypy --strict src`, `pytest -q` on Python 3.12 and 3.14.

```sh
pip install -e ".[dev]"
ruff check src tests && mypy --strict src && pytest -q
```
