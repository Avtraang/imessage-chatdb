# `attributedBody` typedstream: grammar, verified facts, fixtures, reader API

This is the specification for the 0.2 typedstream reader
(`imessage_chatdb.typedstream_reader`) and for the byte-exact fixture writer
in `tests/fixtures/typedstream_writer.py`. Everything the reader needs is here;
the implementers do not need a real database.

**How it was verified.** The grammar below was derived from the public
description of the format (the `pytypedstream` project's reader, whose
constants and reading rules are the reference, and the write-ups around
`imessage-exporter`) and then checked against **every** `attributedBody` on
the author's live macOS 27 Messages database, read-only (`?mode=ro`): 20,783
blobs, 176 to 48,165 bytes (mean 614). A throwaway tokenizer walked each blob
with the rules in sections 1 to 5 and classified every byte; `pytypedstream`
(`unarchive_from_data`) decoded all 20,783 as well; and the fixture writer of
section 8 re-encoded each blob from its parsed structure and was compared
byte-for-byte (section 7). Only counts, tag bytes, offsets, class names and
attribute-key identifiers were ever printed. Facts carry their counts; anything
not seen on the live database is marked **unverified**.

Conventions: byte values are hex (`0x84`); `S[n]` is entry `n` of the shared
string table and `O[n]` entry `n` of the object table (section 2.3).

---

## 1. Header

```
04 0b "streamtyped" 81 e8 03
```

| bytes | meaning |
|---|---|
| `04` | streamer version 4 (the only version macOS writes) |
| `0b` + `streamtyped` | 11-byte signature; `streamtyped` means little-endian (`typedstream` would be big-endian) |
| `81 e8 03` | system version as a typedstream integer: `0x81` + u16 LE = 1000 |

Verified: all 20,783 blobs start with exactly these 16 bytes.

After the header the stream is a sequence of **typed-value groups** (section 3).
An `attributedBody` holds exactly one top-level group, of type `@`, carrying
the `NSAttributedString` (20,783 of 20,783; nothing follows its final `0x86`).

## 2. Primitive encoding

### 2.1 Head bytes and tags

Every variable-width item starts with a *head* byte. Heads in `0x80..0x91`
(signed `-128..-111`) are **tags**; every other head byte is a literal
single-byte integer or a single-byte reference number.

| tag | meaning |
|---|---|
| `0x81` | integer in the next 2 bytes (LE) |
| `0x82` | integer in the next 4 bytes (LE) |
| `0x83` | floating point: the next 4 (`f`) or 8 (`d`) bytes IEEE 754 LE. **Not an integer tag.** Never seen in any blob (0 occurrences in any position). |
| `0x84` | "new": a literal string, object or class follows |
| `0x85` | nil (string, class, object, C string) |
| `0x86` | end of object |
| `0x80`, `0x87..0x91` | reserved; never seen |

### 2.2 Integers

`int(head)`: if the head is not a tag it is the value itself: for a **signed**
context (`i`, `q`, `c`-typed scalars, class versions, reference numbers) the
head byte is a two's-complement int8 (`0xff` = -1); for an **unsigned**
context (string lengths, `I`, dictionary counts) it is `0..255` — in practice
only `0x00..0x7f` ever appears for an unsigned value (see the width table);
the reader follows the reference reader and takes an unsigned head in
`0x92..0xff` as the literal 146..255 (**unverified**: Apple's writer was never
seen to produce it, and the 0.1 `extract_text` returns `None` for such a
length tag, so the oracle rule of section 9.4 is stated over writer-produced
blobs, which never contain one). `0x81` reads int16 LE, `0x82` int32 LE
(signed or unsigned per context). `0x83` in an integer context is an error for the reader
(section 9); the 0.1 `extract_text` byte-scan reads `0x83` + u64 and keeps
doing so, which is harmless because no blob contains it.

Writer rule (reproduces every live integer): a value is written in one byte
when it fits a signed byte and is outside the tag range `-128..-111`, else
`0x81` + int16 when it fits int16, else `0x82` + int32. Hence unsigned 128..255
is `0x81` + 2 bytes and signed -1 is a single `0xff`.

Verified widths by site (every blob):

| site | one byte `< 0x80` | one byte `>= 0x92` | `0x81` | `0x82` | `0x83` |
|---|---|---|---|---|---|
| `+` string length (text, keys, values) | 72,654 | 0 | 3,306 | 0 | 0 |
| `I` run length | 28,593 | 0 | 2,455 | 0 | 0 |
| `i` (dict index, dict count, NSData length, NSNumber `i`) | 74,480 | 0 | 7,529 | 0 | 0 |
| `q` NSNumber | 6,084 | 5,512 (all negative: writing direction -1) | 0 | 1 | 0 |
| class name length, class version, type-string length | 151,459 / 151,459 / 112,087 | 0 | 0 | 0 | 0 |
| reference numbers | 14 (indices 110..112) | 461,301 (indices 0..109) | 0 | 0 | 0 |
| system version | 0 | 0 | 20,783 | 0 | 0 |

Text length buckets: `< 128` bytes 17,649 blobs; 128..145: 512; 146..255:
1,636; `>= 256`: 986 — every length `>= 128` used `0x81`. Longest text 9,966
bytes (so `0x82` for a text length is **unverified**, as is any length in
32,768..65,535: whether Apple writes it as `0x81` + u16 or `0x82` + int32 is
unknown; the reader accepts both because it reads `0x81` payloads unsigned in
unsigned contexts).

### 2.3 References and the two shared tables

A reference is an integer `r` (section 2.2, signed) standing for index
`r - (-110)`, i.e. `r + 110`. Single-byte heads `0x92..0xff` are indices
0..109; heads `0x00..0x7f` in a reference position are indices 110..237
(seen 14 times, maximum index 112); larger indices use `0x81` + int16
(**unverified**, never needed: the largest object table was 250 entries but no
blob referenced an entry above 112).

There are two independent tables, both filled in stream order by the reader:

- **Shared strings** `S`: every type string, class name and C-string body
  written literally (`0x84` + length + bytes) is appended. A later use of the
  same string is a reference into `S`. Largest `S` seen: 29 entries; highest
  reference 26.
- **Objects** `O`: one slot per literal object (taken *before* its class is
  read), per literal class (in the order they appear in the chain), and per
  literal C string (`*` value). A reference in an object position, a class
  position or a C-string position indexes `O`. Largest `O` seen: 250.

The reader must resolve a reference to the already-built value without
re-parsing (so a reference can never recurse), and must raise on an index that
is out of range or of the wrong kind (class where an object is expected, etc.).

### 2.4 Strings

- **Unshared string** (the value of a `+` group): `0x85` for nil, else
  `uint` length + that many bytes. No `0x84` precedes it. This is how every
  `NSString` body is written: UTF-8, no terminator. 20,783 of 20,783 texts
  decode as UTF-8 (`errors="replace"` stays, for corrupt rows).
- **Shared string** (type strings, class names, C-string bodies): `0x85` nil;
  `0x84` + unshared string, appended to `S`; else a reference into `S`.
- **C string** (the value of a `*` group): `0x85` nil; `0x84` + shared string,
  and a slot in `O`; else a reference into `O` (observed: 8,239 literal,
  ~22,000 references; the same `"i"` objCType pointer is reused).

## 3. Typed-value groups and type strings

A group is a shared string (the *type string*) followed by one value per
type character. Type strings seen on the live database (occurrences):

| type | values | seen |
|---|---|---|
| `@` | object (section 4) | 178,723 |
| `+` | unshared string | 75,960 |
| `i` | signed int | 50,961 |
| `iI` | two values: signed int, unsigned int (the run pair, section 5) | 31,048 |
| `*` | C string | 30,614 |
| `q` | signed int64 (same integer encoding) | 11,597 |
| `c` | **one raw byte, no head** (1 occurrence = 1 byte) | 6,655 |
| `[Nc]` (e.g. `[572c]`) | `N` raw bytes (the `NSData` payload); `N` is decimal in the type string and the type string itself is shared like any other | 6,083 |

Not seen, defined by the reference reader, and to be accepted or rejected as
noted: `s`/`l` (int, same encoding as `i`), `S`/`L`/`Q` (unsigned), `C`/`B`
(one raw byte), `f`/`d` (`0x83` + 4/8 bytes, or an integer head for a whole
number), `#` (a class, section 4), `%`/`:` (shared string), `[N<type>]` for a
non-byte element, `{...}` structs, `!`. The reader raises `TypedStreamError`
for `[`/`{` with a non-byte element and for any other unknown type character
(it cannot know the width), and reads the scalar ones.

## 4. Objects and classes

```
object  := 0x85                                   nil
         | 0x84 class-chain group* 0x86           literal (takes O slot first)
         | ref                                    reference into O (an object)
class-chain := (0x84 shared-string version)+ (0x85 | ref)
```

A literal object is `0x84`, then its class chain, then zero or more typed-value
groups (its fields, in `encodeWithCoder:` order), then `0x86`. The chain lists
the class and its superclasses, most derived first, each as `0x84` + name
(shared string) + version (signed int), and ends with `0x85` when the last
literal class is a root (`NSObject`) or with a reference into `O` to a class
written earlier (the superclass). Each literal class takes an `O` slot in
stream order, after the object's own slot.

Class versions seen (constant across all blobs): `NSObject` 0, `NSString` 1,
`NSMutableString` 1, `NSAttributedString` 0, `NSMutableAttributedString` 0,
`NSDictionary` 0, `NSNumber` 0, `NSValue` 0, `NSURL` 0, `NSData` 0,
`NSMutableData` 0, `NSMutableDictionary` 0 (1 blob), `NSArray` 0 (1 blob).
Deepest object nesting: 10.

## 5. `NSAttributedString` layout

Root class chain: `NSAttributedString > NSObject` (14,721 blobs) or
`NSMutableAttributedString > NSAttributedString > NSObject` (6,062). Fields:

1. group `@`: the string, `NSString > NSObject` (14,721) or
   `NSMutableString > NSString > NSObject` (6,062), whose single field is a
   `+` group with the UTF-8 text. The plain and mutable roots go together
   (plain root always has `NSString`, mutable root always `NSMutableString`).
2. Then, for each **attribute run** in order, a group `iI` = **(dictionary
   index, run length)**:
   - `i` is the 1-based index of the run's attribute dictionary in the
     per-archive table of distinct dictionaries;
   - `I` is the run length in **UTF-16 code units**;
   - when the index is one past the number of dictionaries seen so far (a new
     dictionary), a group `@` with the `NSDictionary` follows the pair; when
     the index points back at an earlier dictionary nothing follows.
3. `0x86`.

Verified on 20,783 of 20,783 blobs: the index rule holds exactly (never a
dictionary after a back-reference, never a missing dictionary after a new
index, indices never skip) and the run lengths sum to the UTF-16 length of the
text. Run counts per blob: 1 run 16,798; 2: 1,160; 3: 1,774; 4: 231; 5: 472;
6..10: 290; 11..20: 46; more than 20: 12 (max 117). Distinct dictionaries per
blob: 1: 16,799; 2: 2,834; 3..5: 1,054; 6..20: 93; more: 4 (max 60). 4,192 of
the 31,049 runs were back-references to an earlier dictionary. Longest run
4,872 units.

The 0.1 fixture writer has these two integers the other way round (run
length, then `1`) — identical bytes only when the text is one code unit long.

## 6. Attribute dictionaries

`NSDictionary > NSObject`, fields: group `i` = pair count `n` (seen 1..5), then
`n` times: group `@` key (an `NSString` object), group `@` value. Key order is
`NSDictionary` enumeration order: the same five-key set was seen in three
different orders, so the reader must treat the pairs as a mapping and never
rely on order. Repeated key strings are sometimes a reference to the earlier
key *object* and sometimes a fresh `NSString` (section 7): the reader must
accept both (it does naturally, as both are `@` values).

Keys seen (occurrences across all dictionaries, including nested), with the
value class and its layout:

| key | seen | value |
|---|---|---|
| `__kIMMessagePartAttributeName` | 26,822 | `NSNumber` `i` (20,449) or `q` (6,373): the 0-based part index |
| `__kIMBaseWritingDirectionAttributeName` | 7,699 | `NSNumber` `q` = -1 (7,526); `NSString` (173) |
| `__kIMLinkAttributeName` | 3,921 | `NSURL` |
| `__kIMDataDetectedAttributeName` | 3,898 | `NSMutableData` / `NSData` (a keyed archive of the data detector result) |
| `__kIMFileTransferGUIDAttributeName` | 3,146 | `NSString`: the attachment guid; the run is the one-unit U+FFFC placeholder (3,143 of 3,148 runs; 5 runs are longer / not a placeholder, and 5 placeholders carry no guid) |
| `__kIMCalendarEventAttributeName` | 3,005 | `NSMutableData` / `NSData` |
| `__kIMLinkIsRichLinkAttributeName` | 2,730 | `NSNumber` `c` = 1 |
| `__kIMPhoneNumberAttributeName` | 324 | `NSMutableData` |
| `__kIMOneTimeCodeAttributeName` | 278 | nested `NSDictionary` of 2–3 `NSString` pairs |
| `__kIMMoneyAttributeName` | 228 | `NSMutableData` |
| `__kIMFilenameAttributeName` | 173 | `NSString` |
| `__kIMMentionConfirmedMention` | 89 | `NSString`: the mentioned handle; the run text is the display name (none of the 89 runs starts with `@`) |
| `__kIMAddressAttributeName` | 75 | `NSMutableData` |
| `__kIMTextBoldAttributeName` | 64 | `NSNumber` `i` (1, or 0 when cleared) |
| `__kIMBreadcrumbTextMarkerAttributeName` / `__kIMBreadcrumbTextOptionFlags` | 34 / 34 | `NSString` / `NSNumber` `q`; these 34 runs carry **no** part attribute |
| `__kIMEmojiImageAttributeName` | 25 | `NSNumber` |
| `__kIMTextItalicAttributeName`, `__kIMTextUnderlineAttributeName` | 14 / 14 | `NSNumber` `i` |
| `__kIMPhotoSharingAttributeName` | 11 | empty `NSDictionary` |
| `__kIMTextEffectAttributeName` | 10 | `NSNumber` `i` |
| `__kIMTextStrikethroughAttributeName` | 4 | `NSNumber` `i` |
| `IMAudioTranscription`, `__kIMUrlToTransferMapAttributeName`, `__kIMRichCardsAttributeName` | 1 each | (`NSMutableDictionary`, `NSArray` appear here) |

Part-index behaviour over all 31,047 runs: never decreases; 8,993 runs
continue the previous run's part (text with a link or mention is several runs
of one part); 1 skip; 34 runs without the attribute. The attachment
placeholder is its own part.

Value classes:

- **`NSNumber > NSValue > NSObject`**: group `*` = the Objective-C type as a C
  string (`i`, `q` or `c` seen), then a group of that type with the value.
  First `NSNumber` in an archive: `84 01 2a 84 <S-ref or literal "i"> <S-ref "i"> <value>`;
  later ones reference the C string in `O` and the type string in `S`.
  `c` values are one raw byte (1 seen 2,755 times, 0 never).
- **`NSString > NSObject`**: one `+` group.
- **`NSURL > NSObject`** (3,921): group `c` = one byte `0x00` (always 0;
  presumably "no base URL", **unverified** what a nonzero byte would be
  followed by), then group `@` = `NSString` with the URL string. 3,348 of
  3,921 link runs have text equal to the URL string; 573 differ.
- **`NSData > NSObject`** (1,172) / **`NSMutableData > NSData > NSObject`**
  (4,911): group `i` = length, then group `[Nc]` with `N` raw bytes
  (lengths 534..2,816 seen; the type string `[572c]` is shared, so two
  `NSData` of equal length reference the same `S` entry).
- **nested `NSDictionary`**: as above.
- **`NSMutableDictionary`**, **`NSArray`**: 1 blob each; the reader keeps them
  as `UnknownValue` (section 9) and still returns the text and runs.

## 7. Verification summary and fixture discrepancies

| claim | result |
|---|---|
| blobs scanned | 20,783 = every non-NULL `attributedBody` (not a sample) |
| bytes the tokenizer could not classify | 0 (no unknown tag, no short read, no bad reference) |
| decoded by the reference `pytypedstream` decoder | 20,783 (all `GenericArchivedObject`) |
| root class | `NSAttributedString` 14,721; `NSMutableAttributedString` 6,062 |
| one top-level `@` group, nothing after the final `0x86` | 20,783 |
| `extract_text(blob)` equals the `+` text of the parsed string object | 20,783 (and no blob has an empty text) |
| texts containing U+FFFC | 2,612 |
| `PLAIN_SKELETON` is an exact prefix | 14,721 of 14,721 plain blobs |
| `MUTABLE_SKELETON` (0.1) is an exact prefix | **0** of 6,062 mutable blobs (they diverge at byte 22: the root is `NSMutableAttributedString`) |
| `MUTABLE_ROOT_SKELETON` (section 8) is an exact prefix | 6,062 of 6,062 |
| 0.1 single-run trailer vs live single-run, single-attribute plain blobs | all 11,467 differ in exactly one byte: the key string's `+` type reference is `0x96` live, `0x97` in `TRAILER_TAIL` (`0x97` would name `S[5]` = `iI`); and the `iI` pair is (index, length) live, (length, 1) in the fixture |
| `encode_attributed_body_runs` re-encoding each live blob from (text, runs) | byte-exact for **19,008** of 20,782 with the default flags (16,710 single-run, 2,298 multi-run; 14,227 plain, 4,781 mutable); 17,592 with `share_keys=True`; 16,829 with both flags off; some flag setting is exact for 19,972 (96.1%). The remaining 810 mix pointer-shared and fresh copies of equal objects inside one archive, which bytes alone cannot express; all of them parse. 1 blob (`NSMutableDictionary` value) is outside the writer's value types. |

What the live archiver shares by pointer (so a second encoding is an `O`
reference rather than a fresh object): tagged `NSNumber`s (the part index 0,
writing direction -1, `@YES`) in 2,298 + multi-run blobs; the `NSURL` a data
detector attached to several runs; attribute key strings in 770 blobs but
*not* in 1,954 others (same key name, a different `NSString` instance). The
writer's `share_values` / `share_keys` flags choose between the two.

## 8. Fixture writer (`tests/fixtures/typedstream_writer.py`)

Unchanged: `encode_attributed_body(text, *, mutable=False, force_len_tag=None)`,
`PLAIN_SKELETON`, `MUTABLE_SKELETON`, `TRAILER_HEAD`, `TRAILER_TAIL`, `LEN_TAGS`,
`encode_int`. These feed the 0.1 `extract_text` pins and stay byte-for-byte.
Blobs from `encode_attributed_body` are **not** reader-exact (section 7):
`parse_attributed_body` raises on them (the `0x97` reference makes the key
string's type `iI`), `try_parse_attributed_body` returns `None`. Tests of the
reader use the new writer.

New:

```python
encode_attributed_body_runs(
    text: str,
    runs: list[tuple[int, Mapping[str, object]]],   # (utf16_length, attributes)
    *,
    mutable: bool = False,        # NSMutableAttributedString + NSMutableString root
    share_keys: bool = False,     # repeated key string -> O reference (else fresh NSString)
    share_values: bool = True,    # repeated equal value object -> O reference
) -> bytes
```

- The run lengths must sum to the UTF-16 length of `text` (`ValueError`);
  each distinct dictionary (by `==`) is archived once, on first use, and later
  equal dictionaries are written as a back-reference index.
- Values: `int` -> `NSNumber` `i`; `bool` -> `NSNumber` `c`;
  `Number(value, objc_type)` for an explicit `i`/`q`/`c`; `str` -> `NSString`;
  `bytes` -> `NSData`; `Data(payload, mutable=True)` -> `NSMutableData`;
  `URL(relative)` -> `NSURL`; a `Mapping` -> nested `NSDictionary`. Anything
  else: `TypeError`. Keys are written in mapping order.
- `encode_sint(value)`: the signed integer rule of section 2.2 (used for
  `i`/`q`, versions, references); `encode_int` stays the unsigned one.
- `MUTABLE_ROOT_SKELETON`: the verified mutable prefix (section 7).
- Key constants: `PART`, `FILE_TRANSFER_GUID`, `WRITING_DIRECTION`, `MENTION`,
  `LINK`, `LINK_IS_RICH`, `DATA_DETECTED`, `ONE_TIME_CODE`, `TEXT_BOLD`.

### 8.1 Golden dumps

All three blobs below decode with `pytypedstream`, with the survey tokenizer,
and with `extract_text` (which returns the text). They are pinned by the tests
in `tests/test_typedstream.py`.

**(a)** `encode_attributed_body_runs("hi", [(2, {PART: 0})])` — 177 bytes,
annotated; this is also the exact layout of a live one-part text message:

```
04 0b 73747265616d7479706564 81 e8 03   header, system version 1000
84 01 40                                 S0 = "@"           (type string)
84                                       O0 = new object: NSAttributedString
84 84 12 4e5341...696e67 00              O1 = class NSAttributedString (S1), version 0
84 84 08 4e534f626a656374 00             O2 = class NSObject (S2), version 0
85                                       nil: end of the class chain
92                                       type "@"  (S0)
84                                       O3 = new object: NSString
84 84 08 4e53537472696e67 01             O4 = class NSString (S3), version 1
94                                       superclass = O2 (NSObject)
84 01 2b                                 S4 = "+"
02 6869                                  length 2, "hi"
86                                       end O3
84 02 6949                               S5 = "iI"
01 02                                    dictionary index 1, run length 2
92                                       type "@"
84                                       O5 = new object: NSDictionary
84 84 0c 4e5344696374696f6e617279 00     O6 = class NSDictionary (S6), version 0
94                                       superclass = O2
84 01 69                                 S7 = "i"
01                                       one pair
92                                       type "@"
84                                       O7 = new object (the key)
96                                       class = O4 (NSString)
96                                       type "+" (S4)
1d 5f5f6b494d4d6573...4e616d65           length 29, "__kIMMessagePartAttributeName"
86                                       end O7
92                                       type "@"
84                                       O8 = new object: NSNumber
84 84 08 4e534e756d626572 00             O9 = class NSNumber (S8)
84 84 07 4e5356616c7565 00               O10 = class NSValue (S9)
94                                       superclass = O2
84 01 2a                                 S10 = "*"
84 99                                    O11 = C string, body = S7 ("i")
99                                       type "i" (S7)
00                                       value 0
86 86 86                                 end O8, end O5, end O0
```

Full hex:

```
040b73747265616d747970656481e803840140848484124e5341747472696275746564537472696e67008484084e534f626a656374008592848484084e53537472696e67019484012b02686986840269490102928484840c4e5344696374696f6e617279009484016901928496961d5f5f6b494d4d657373616765506172744174747269627574654e616d658692848484084e534e756d626572008484074e5356616c7565009484012a84999900868686
```

**(b)** the same with `mutable=True` — 225 bytes; the class references shift
by one (`NSObject` is `O3`, so `0x95`; `NSString` is `O6`, so the key's class
is `0x98`; `"+"` is `S6`, so `0x98`; `"i"` is `S9`, so `0x9b`):

```
040b73747265616d747970656481e803840140848484194e534d757461626c6541747472696275746564537472696e67008484124e5341747472696275746564537472696e67008484084e534f626a6563740085928484840f4e534d757461626c65537472696e67018484084e53537472696e67019584012b02686986840269490102928484840c4e5344696374696f6e617279009584016901928498981d5f5f6b494d4d657373616765506172744174747269627574654e616d658692848484084e534e756d626572008484074e5356616c7565009584012a849b9b00868686
```

**(c)** three parts in a mutable blob: an attachment placeholder with a fake
guid, text, a mention, text, a link (five runs, 694 bytes with the default
flags; 595 bytes with `share_keys=True`):

```python
text = "￼ see @Sam https://example.test/x"      # 33 UTF-16 units
runs = [
    (1,  {FILE_TRANSFER_GUID: "AT_0_0000-FAKE-GUID", WRITING_DIRECTION: Number(-1, "q"), PART: 0}),
    (5,  {PART: 1}),
    (4,  {PART: 1, MENTION: "+15550100"}),
    (1,  {PART: 1}),
    (22, {LINK: URL("https://example.test/x"), PART: 1, LINK_IS_RICH: True}),
]
encode_attributed_body_runs(text, runs, mutable=True)
```

```
040b73747265616d747970656481e803840140848484194e534d757461626c6541747472696275746564537472696e67008484124e5341747472696275746564537472696e67008484084e534f626a6563740085928484840f4e534d757461626c65537472696e67018484084e53537472696e67019584012b23efbfbc20736565204053616d2068747470733a2f2f6578616d706c652e746573742f7886840269490101928484840c4e5344696374696f6e61727900958401690392849898225f5f6b494d46696c655472616e73666572475549444174747269627574654e616d6586928498981341545f305f303030302d46414b452d475549448692849898265f5f6b494d4261736557726974696e67446972656374696f6e4174747269627574654e616d658692848484084e534e756d626572008484074e5356616c7565009584012a848401719fff86928498981d5f5f6b494d4d657373616765506172744174747269627574654e616d658692849f9e849b9b00868699020592849a9b01928498981d5f5f6b494d4d657373616765506172744174747269627574654e616d658692849f9ea49b01868699030492849a9b02928498981d5f5f6b494d4d657373616765506172744174747269627574654e616d658692a7928498981c5f5f6b494d4d656e74696f6e436f6e6669726d65644d656e74696f6e8692849898092b3135353530313030868699020199041692849a9b0392849898165f5f6b494d4c696e6b4174747269627574654e616d658692848484054e5355524c009584016300928498981668747470733a2f2f6578616d706c652e746573742f788686928498981d5f5f6b494d4d657373616765506172744174747269627574654e616d658692a792849898205f5f6b494d4c696e6b4973526963684c696e6b4174747269627574654e616d658692849f9e84a1a101868686
```

Things to notice in (c): `99 02 05` is run 2 = (dictionary 2, length 5); `99 02 01`
is run 4 = (dictionary 2 again, length 1) with no dictionary following; the
later copies of `NSNumber(1, "i")` (dictionaries 3 and 5) are the single
reference byte `0xa7` (`O21`); the
writing direction `-1` of type `q` is `84 84 01 71 9f ff` (C string literal
whose body is the new shared string `"q"`, type `q` by reference, then `0xff`); the `NSURL` is `84 84 84 05 4e5355524c 00 95 84 01 63 00 92 ...`
(class, `c` byte 0, then the `NSString`); the `c` NSNumber for the rich-link
flag is `84 a1 a1 01` (C string literal whose body is the shared `"c"` already
in `S` from the `NSURL`, type `c` by reference, one raw byte).
With `share_keys=True` the repeated `__kIMMessagePartAttributeName` keys become
`92 a2` (type `@`, reference `O16`).

**(d)** `NSData` and a nested dictionary (345 bytes):
`encode_attributed_body_runs("ab", [(1, {PART: 0, DATA_DETECTED: b"\x00\x01\x02"}), (1, {PART: 0, ONE_TIME_CODE: {"code": "1234"}})])`:

```
040b73747265616d747970656481e803840140848484124e5341747472696275746564537472696e67008484084e534f626a656374008592848484084e53537472696e67019484012b02616286840269490101928484840c4e5344696374696f6e617279009484016902928496961d5f5f6b494d4d657373616765506172744174747269627574654e616d658692848484084e534e756d626572008484074e5356616c7565009484012a8499990086928496961e5f5f6b494d4461746144657465637465644174747269627574654e616d658692848484064e53446174610094990384045b33635d00010286869702019284989902928496961d5f5f6b494d4d657373616765506172744174747269627574654e616d6586929a928496961d5f5f6b494d4f6e6554696d65436f64654174747269627574654e616d658692849899019284969604636f64658692849696043132333486868686
```

Notice `84 04 5b33635d 000102`: the `[3c]` type string, then three raw bytes;
and `92 9a` in dictionary 2: the shared `NSNumber(0)` as reference `O8`.

## 9. Reader API (`src/imessage_chatdb/typedstream_reader.py`)

New module; `typedstream.py` and its three public functions are untouched and
`extract_text` stays the oracle. Nothing here opens a database. No runtime
dependency. All public names are added to `imessage_chatdb.__all__`
(additive; 0.1 callers keep working).

```python
class TypedStreamError(ValueError):
    """Malformed typedstream.  Message says the byte offset and what was expected."""

@dataclass(frozen=True, slots=True)
class Url:
    relative: str                  # NSURL: the archived relative string; base is always nil (section 6)

@dataclass(frozen=True, slots=True)
class UnknownValue:
    class_name: str                # e.g. "NSMutableDictionary", "NSArray"; fields are not interpreted

AttributeValue = int | str | bytes | Url | Mapping[str, "AttributeValue"] | UnknownValue | None

@dataclass(frozen=True, slots=True)
class AttributeRun:
    start: int                     # UTF-16 code-unit offset into AttributedBody.text, inclusive
    end: int                       # exclusive; end - start is the archived run length
    attributes: Mapping[str, AttributeValue]   # read-only (types.MappingProxyType over a dict)
    def chars(self, text: str) -> str: ...     # the run's slice of ``text`` (Python code points)

@dataclass(frozen=True, slots=True)
class AttributedBody:
    text: str
    runs: tuple[AttributeRun, ...] # in order, contiguous, runs[0].start == 0, runs[-1].end == utf16 length
    root_class: str                # "NSAttributedString" | "NSMutableAttributedString"
    mutable: bool                  # root_class == "NSMutableAttributedString"

def parse_attributed_body(data: bytes) -> AttributedBody: ...
def try_parse_attributed_body(data: bytes | None) -> AttributedBody | None: ...

@dataclass(frozen=True, slots=True)
class Mention:
    handle: str                    # __kIMMentionConfirmedMention value
    start: int                     # code-point offsets into the owning TextPart.text
    end: int

@dataclass(frozen=True, slots=True)
class TextPart:
    text: str
    part_index: int | None
    mentions: tuple[Mention, ...]
    links: tuple[str, ...]         # __kIMLinkAttributeName URL strings, first-seen order, deduplicated

@dataclass(frozen=True, slots=True)
class AttachmentPart:
    guid: str                      # __kIMFileTransferGUIDAttributeName
    part_index: int | None

@dataclass(frozen=True, slots=True)
class UnknownPart:
    part_index: int | None
    attributes: Mapping[str, AttributeValue]

Part = TextPart | AttachmentPart | UnknownPart

def message_parts(body: AttributedBody) -> tuple[Part, ...]: ...
```

### 9.1 `parse_attributed_body`

Rules, in the order the implementation meets them:

1. Header exactly as section 1 (streamer 4, `streamtyped`; system version read
   as an unsigned integer and required to be 1000). Big-endian `typedstream`
   and streamer 3 raise `TypedStreamError` (never seen; not worth supporting).
2. One typed-value group of type `@` whose object's class chain starts with
   `NSAttributedString` or `NSMutableAttributedString` (`root_class`). Any
   trailing bytes after its `0x86` raise. (Zero live blobs have trailing bytes;
   being strict here is what keeps the oracle test honest.)
3. Field 1 must be an `@` group holding an `NSString`/`NSMutableString` object
   with exactly one `+` field: `text` = those bytes decoded as UTF-8 with
   `errors="replace"` (so `text == extract_text(data)` whenever the latter is
   not `None`; see 9.4 for the empty case).
4. Remaining fields: `iI` groups per section 5, each optionally followed by an
   `@` group; the index rule is enforced (a new index must be exactly
   `len(dicts) + 1` and must be followed by a dictionary; a back-reference must
   not be followed by `@`; any other field type raises). Lengths must sum to
   the UTF-16 length of `text`; otherwise raise. Runs are built with
   cumulative `start`/`end`.
5. A dictionary value object is converted: `NSString` -> `str`;
   `NSNumber` -> `int` (the `c` byte, `i`/`q` integer); `NSURL` -> `Url`;
   `NSData`/`NSMutableData` -> `bytes`; `NSDictionary` -> `Mapping`; nil ->
   `None`; any other class -> `UnknownValue(class_name)` after its fields have
   been *skipped structurally* (parsed with the generic grammar and discarded).
   A *known* class whose fields are not laid out as above (an `NSString`
   without exactly one `+` field or with a nil string, a nested
   `NSDictionary` whose pair count does not match its fields, whose key is
   not an `NSString` or whose pair is not two objects, an `NSURL` whose
   string is broken, an `NSNumber`/`NSData` with the wrong groups) also
   becomes `UnknownValue(class_name)` **in value position**: the text and the
   other attributes are unaffected. Three positions stay strict and raise:
   the root's text string, a dictionary key, and a run dictionary itself
   (its pair count, its keys, its pairs). Duplicate keys: last wins. Each
   archived object is converted once; the result is cached on the object, so
   a string or dictionary that many runs or pairs reference by O-reference is
   decoded once and the same Python object is shared (`runs[i].attributes is
   runs[j].attributes` for runs that reuse one archived dictionary, and the
   same holds for a referenced value inside them).
6. Generic grammar for anything not special-cased (so unknown objects inside a
   known attribute do not break the text): sections 2–4 verbatim, including
   `[Nc]` byte arrays, `#`, `%`, `:`, `f`/`d`, `B`/`C`/`c`, `s`/`S`/`l`/`L`/`q`/`Q`.
7. Limits (all raise `TypedStreamError`): literal object nesting depth
   `> 64` (observed 10); a class chain longer than 64 classes, references
   included (observed 3; the chain is read iteratively, so a blob of
   thousands of chained classes raises instead of overflowing the stack);
   converted attribute values nested more than 64 mappings deep, counted
   through back-references as well as literals (a dictionary referenced from
   another dictionary adds its own height; observed 2) — the one conversion
   error that is never softened to `UnknownValue`, since `to_dict()` and
   `json.dumps` could not walk a deeper result; any read past the end of
   `data`; a reference out of range or of the wrong kind; a `0x83` head in an
   integer context; an unknown type character; a `[`/`{` type with a
   non-byte element; total runs `> len(data)` cannot happen since every run
   costs at least 3 bytes, but the loop is nonetheless bounded by the input:
   every iteration of every loop consumes at least one byte or raises,
   references are table lookups, and every object is converted at most once,
   so parsing is `O(len(data))` in time and memory and cannot hang. No depth
   is bounded by the interpreter's recursion limit: every cap is a counter.
8. `try_parse_attributed_body` returns `None` for `None`/empty input and for
   any `TypedStreamError` (or `UnicodeDecodeError`, which cannot occur with
   `errors="replace"` but is caught for safety, or `RecursionError`, which
   the counters above make unreachable but which is caught as a backstop so
   that `Message.body` and `tail --parts` can never raise); it never raises.

### 9.2 `AttributeRun.chars` and UTF-16 offsets

Offsets are UTF-16 code units because that is what the archive stores. `chars(text)`
converts: when the text has no astral code point (one unit per code point,
checked with one C-speed encode) the offsets are plain slice indexes;
otherwise it walks once computing the code-point index whose cumulative unit
count reaches `start`/`end` (a non-BMP code point is 2 units). A run boundary
never falls inside a surrogate pair on a well-formed archive; if it did,
`chars` rounds the boundary down to the code point containing it (documented,
tested with a synthetic blob whose lengths split an emoji). `message_parts`
does not call `chars` per run: it builds one unit -> code-point table per
body and slices every run from it, so it is `O(len(text) + len(runs))` rather
than runs x text (a 200 KB text with 2,000 runs converts in milliseconds).

### 9.3 `message_parts`

Group consecutive runs by their `__kIMMessagePartAttributeName` value
(`None` when absent) into parts, in order:

- a part whose runs are a single one-unit run of U+FFFC carrying
  `__kIMFileTransferGUIDAttributeName` (a `str`) -> `AttachmentPart(guid, part_index)`;
- otherwise, if every run of the part has text (any run is fine; a part with
  a U+FFFC but no guid, 5 live runs, is text) -> `TextPart(text=concatenated
  run text, part_index, mentions, links)` where `mentions` are the runs with a
  `str` `__kIMMentionConfirmedMention`, offsets relative to the part's text,
  and `links` the `Url.relative` strings of `__kIMLinkAttributeName` in first-seen
  order without duplicates;
- `UnknownPart(part_index, attributes)` for a part made of a single empty run
  or a run whose only attributes are not text-related (the 34 breadcrumb runs:
  no part attribute, `__kIMBreadcrumbTextMarkerAttributeName` present) — the
  merged attributes of its runs (later runs win).

A part index that decreases does not occur (verified); if it does, a new part
starts anyway (grouping is on *consecutive equal* values, not on sorting).

### 9.4 Oracle and compatibility rules (tested)

- For every blob from `encode_attributed_body_runs` (any runs, both roots,
  both flag settings, and every text and length case the 0.1 tests use: 1,
  127, 128, 146, 255, 256, 65,535, 65,536 and 70,000 bytes, emoji, Hebrew,
  U+FFFC, invalid UTF-8 placed by hand):
  `parse_attributed_body(data).text == extract_text(data)`.
- Empty text: `extract_text` returns `None` for `""` (0.1 rule, unchanged);
  `parse_attributed_body` returns `text == ""` with one zero-length run. The
  oracle test therefore compares `extract_text(data) == (body.text or None)`.
- `effective_text` is untouched and never consults the reader.
- Truncation: for every prefix of a well-formed blob, `parse_attributed_body`
  raises `TypedStreamError` (the trailer is required) and `try_parse_...`
  returns `None`; `extract_text` keeps its 0.1 behaviour (text complete ->
  text). The `.text` of a parsed blob is never a partial string.
- Fuzz: random bytes and bit-flipped golden blobs (seeded, 2,000 iterations
  as in `test_fuzz.py`) never raise anything but `TypedStreamError` from
  `parse_attributed_body`, never hang, and `try_parse_attributed_body` returns
  `None` or an `AttributedBody`.
- `encode_attributed_body` (0.1 writer) blobs: `parse_attributed_body` raises
  (section 8); pinned so the discrepancy stays documented until that writer is
  retired.

### 9.5 `Message.body`, `Message.parts`, CLI `--parts`

- `Message` gains `attributed_body_raw: bytes | None = None` (keyword-only,
  defaulted, so existing constructors and `dataclasses.replace` keep working;
  `row_to_message` fills it from the row's `attributed_body`, the column the
  SELECT already carries). `to_dict()` is unchanged: no new key.
- `Message.body` -> `AttributedBody | None`: `try_parse_attributed_body(attributed_body_raw)`,
  computed on first access and cached. Because `Message` is
  `frozen=True, slots=True`, `functools.cached_property` cannot be used;
  the cache is a private slot `_body_cache: AttributedBody | None | _Unset`
  set with `object.__setattr__` (same trick for `_parts_cache`), excluded from
  `__eq__`/`__repr__` with `field(default=_UNSET, init=False, repr=False, compare=False)`.
- `Message.parts` -> `tuple[Part, ...]`: `message_parts(body)` or `()` when
  `body` is `None`.
- `Message.text` is still `effective_text(...)`; `body.text` may differ from
  it only when `text_column` is set (the column wins) — documented.
- CLI: `tail` and `search` accept `--parts`; without it the output is
  byte-identical to 0.1.1. With it, each `tail` line gains a `"parts"` key:
  a list of `{"kind": "text", "part_index", "text", "mentions": [{"handle",
  "start", "end"}], "links": [...]}` / `{"kind": "attachment", "part_index",
  "guid"}` / `{"kind": "unknown", "part_index", "attributes": {key: value}}`
  with `bytes` values rendered as `{"base64": ...}`, `Url` as its string, and
  `UnknownValue` as `{"class": name}`; `search` lines gain the same key built
  from the hit's message (one extra `message()` fetch per hit is acceptable;
  or carry `attributed_body` in the hit — implementer's choice, documented).
- `imessage_chatdb.__init__` exports: `TypedStreamError`, `AttributedBody`,
  `AttributeRun`, `Url`, `UnknownValue`, `Mention`, `TextPart`,
  `AttachmentPart`, `UnknownPart`, `parse_attributed_body`,
  `try_parse_attributed_body`, `message_parts`.

### 9.6 Tests the implementation must add (all synthetic)

`tests/test_typedstream_reader.py`: golden blobs (a)–(d) parse to the expected
`AttributedBody` (text, runs with offsets, attribute values incl. `Url`,
`bytes`, nested mapping, `Number("q")` -> -1, `True` -> 1); reference
mechanism (both `share_*` settings parse to the same `AttributedBody`); the
mutable root; UTF-16 offsets with emoji (`chars`); `message_parts` on (c)
gives `(AttachmentPart("AT_0_0000-FAKE-GUID", 0), TextPart(" see @Sam https://example.test/x", 1, mentions=(Mention("+15550100", 5, 9),), links=("https://example.test/x",)))`;
breadcrumb-style run -> `UnknownPart`; the oracle, truncation, fuzz and
0.1-writer rules of 9.4; every limit in 9.1 item 7 hit by a hand-built blob;
`Message.body`/`.parts` via a fixture database row written with
`encode_attributed_body_runs`; `to_dict()` unchanged; CLI `--parts` adds the
key and its absence leaves the line unchanged (byte compare).

## 10. Unverified or open

- `0x82`/`0x83` for a string length, lengths 32,768..65,535, references
  `>= 238` (`0x81` form), unsigned single-byte heads `>= 0x92`, any `f`/`d`
  value, big-endian or streamer-3 archives, a nonzero `NSURL` flag byte, nil
  attribute values: none seen; the reader's handling is specified above but
  only synthetically tested.
- Whether the archiver shares a key string by pointer depends on how the
  message was built (770 blobs share, 1,954 do not); no correlation was
  checked beyond `is_from_me` (both patterns occur for both directions).
- `__kIMDataDetectedAttributeName` and friends are keyed archives (bplists)
  inside `NSData`; decoding them is out of scope (the `KeyedArchive` class
  could be pointed at the bytes by a caller).
- The 0.1 writer's three discrepancies (section 7) are left in place for the
  0.1 pins; retiring `encode_attributed_body` in favour of
  `encode_attributed_body_runs` is a later, test-only change.
