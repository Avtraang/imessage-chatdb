"""Tests for ``python -m imessage_chatdb`` (DESIGN.md 4.7, 8.4 CLI; T9).

``main(argv)`` is driven in-process against synthetic databases under
``tmp_path``; every subcommand gets ``--db PATH`` so the real database is never
touched.  Output is parsed back as JSON lines.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from imessage_chatdb.__main__ import (
    EXIT_ACCESS,
    EXIT_ERROR,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    UsageError,
    build_parser,
    main,
)
from imessage_chatdb.connection import DEFAULT_CHATDB
from tests.conftest import FixtureDB, MakeDB, Pristine, sha256_of
from tests.fixtures import builders as b
from tests.fixtures.typedstream_writer import encode_attributed_body

PHONE = "+15550001234"
OTHER = "+15550009876"
CHAT = "any;-;+15550001234"
GROUP = "any;+;chat9001"


def run(argv: list[str]) -> tuple[int, list[dict[str, Any]], str]:
    """Run ``main`` with captured streams; return ``(code, parsed stdout lines, stderr)``."""
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, stdout=out, stderr=err)
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line]
    return code, lines, err.getvalue()


def populate(fx: FixtureDB) -> dict[str, int]:
    """Two chats, four messages (one blob-only, one tapback)."""
    w = fx.writer
    c1 = b.add_chat(w, CHAT, 45, handles=[PHONE])
    c2 = b.add_chat(w, GROUP, 43, display_name="Synthetic Group", handles=[PHONE, OTHER])
    m1 = b.add_message(w, c1, text="hello from fixture", handle=PHONE)
    m2 = b.add_message(w, c1, text="reply from me", is_from_me=1)
    m3 = b.add_message(w, c2, body=encode_attributed_body("blob only hello"), handle=OTHER)
    m4 = b.add_message(w, c2, assoc_guid=f"p:0/SYN-MSG-{m3:06d}", assoc_type=2000, handle=PHONE)
    return {"c1": c1, "c2": c2, "m1": m1, "m2": m2, "m3": m3, "m4": m4}


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def test_parser_has_db_option_on_every_subcommand() -> None:
    parser = build_parser()
    for argv in (["check"], ["tail"], ["chats"], ["search", "x"]):
        ns = parser.parse_args([*argv, "--db", "/tmp/x.db"])
        assert ns.db == "/tmp/x.db"
        assert parser.parse_args(argv).db == DEFAULT_CHATDB


def test_parser_requires_subcommand() -> None:
    with pytest.raises(SystemExit) as info:
        build_parser().parse_args([])
    # A UsageError is a SystemExit (callers of build_parser keep working) whose
    # code is 64, never argparse's 2 (reserved for "cannot open the database").
    assert isinstance(info.value, UsageError)
    assert info.value.code == EXIT_USAGE == 64
    assert "required" in info.value.message or "arguments" in info.value.message
    assert info.value.usage.startswith("usage:")


# ---------------------------------------------------------------------------
# exit-code contract
# ---------------------------------------------------------------------------


def test_exit_codes_are_distinct() -> None:
    assert len({EXIT_OK, EXIT_ERROR, EXIT_ACCESS, EXIT_USAGE, EXIT_INTERRUPTED}) == 5
    assert EXIT_ACCESS == 2 and EXIT_USAGE == 64


@pytest.mark.parametrize(
    "argv",
    [
        [],  # no subcommand
        ["chekc"],  # typo in the subcommand
        ["tail", "--since-rowid", "abc"],  # bad value
        ["chats", "--bogus"],  # unknown flag
        ["search"],  # missing positional
        ["check", "--db"],  # flag without its value
    ],
)
def test_usage_errors_return_64_not_2_and_never_raise(argv: list[str]) -> None:
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, stdout=out, stderr=err)  # must return, not raise SystemExit
    assert code == EXIT_USAGE
    assert code != EXIT_ACCESS
    assert out.getvalue() == ""
    text = err.getvalue()
    assert text.startswith("usage:")
    assert ": error: " in text
    assert "Full Disk Access" not in text


def test_help_returns_0(capsys: pytest.CaptureFixture[str]) -> None:
    err = io.StringIO()
    assert main(["--help"], stderr=err) == EXIT_OK
    assert main(["tail", "--help"], stderr=err) == EXIT_OK
    assert err.getvalue() == ""
    assert "--since-rowid" in capsys.readouterr().out


def test_usage_error_exit_code_from_a_subprocess() -> None:
    """``python -m imessage_chatdb chekc`` exits 64 (not 2) at the OS level too."""
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-m", "imessage_chatdb", "chekc"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == EXIT_USAGE, proc.stderr
    assert proc.stderr.startswith("usage:")


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def test_check_reports_profile_and_max_rowid(fixture_db: FixtureDB) -> None:
    ids = populate(fixture_db)
    code, lines, err = run(["check", "--db", str(fixture_db.path)])
    assert code == EXIT_OK
    assert err == ""
    assert len(lines) == 1
    info = lines[0]
    assert info["ok"] is True
    assert info["profile"] == fixture_db.profile
    assert info["max_rowid"] == ids["m4"]
    assert info["message"] == "Full Disk Access OK"
    assert info["path"] == str(fixture_db.path)
    assert isinstance(info["optional_missing"], list)


def test_check_on_empty_database(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    code, lines, _ = run(["check", "--db", str(fx.path)])
    assert code == EXIT_OK
    assert lines[0]["max_rowid"] == 0


def test_missing_database_exits_2_with_access_message(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "chat.db"
    code, lines, err = run(["check", "--db", str(missing)])
    assert code == EXIT_ACCESS
    assert lines == []
    assert err.startswith("error: ")
    assert sys.executable in err
    assert "Full Disk Access" in err


def test_not_a_database_exits_2(tmp_path: Path) -> None:
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not sqlite at all" * 100)
    code, _, err = run(["chats", "--db", str(junk)])
    assert code == EXIT_ACCESS
    assert "Full Disk Access" in err


# ---------------------------------------------------------------------------
# tail
# ---------------------------------------------------------------------------


def test_tail_since_rowid_prints_message_dicts_in_rowid_order(fixture_db: FixtureDB) -> None:
    ids = populate(fixture_db)
    code, lines, _ = run(["tail", "--since-rowid", "0", "--db", str(fixture_db.path)])
    assert code == EXIT_OK
    assert [m["rowid"] for m in lines] == [ids["m1"], ids["m2"], ids["m3"], ids["m4"]]
    first = lines[0]
    assert list(first) == [
        "rowid", "guid", "text", "date", "date_read", "date_edited", "is_from_me",
        "sender", "sender_handle", "chat_guid", "chat_name", "is_group", "has_attachments",
        "assoc_guid", "assoc_type", "attachments", "link", "reply_to_guid", "reply_to",
        "service",
    ]  # fmt: skip
    assert first["text"] == "hello from fixture"
    assert first["sender_handle"] == PHONE
    assert first["is_from_me"] is False
    assert first["chat_guid"] == CHAT
    assert isinstance(first["date"], float)
    assert lines[2]["text"] == "blob only hello"
    assert lines[2]["is_group"] is True
    assert lines[2]["chat_name"] == "Synthetic Group"
    assert lines[3]["assoc_type"] == 2000


def test_tail_since_rowid_is_strict(fixture_db: FixtureDB) -> None:
    ids = populate(fixture_db)
    _, lines, _ = run(["tail", "--since-rowid", str(ids["m2"]), "--db", str(fixture_db.path)])
    assert [m["rowid"] for m in lines] == [ids["m3"], ids["m4"]]


def test_tail_limit(fixture_db: FixtureDB) -> None:
    ids = populate(fixture_db)
    _, lines, _ = run(["tail", "--since-rowid", "0", "--limit", "2", "--db", str(fixture_db.path)])
    assert [m["rowid"] for m in lines] == [ids["m1"], ids["m2"]]


def test_tail_without_since_starts_at_now(fixture_db: FixtureDB) -> None:
    populate(fixture_db)
    code, lines, _ = run(["tail", "--db", str(fixture_db.path)])
    assert code == EXIT_OK
    assert lines == []


def test_tail_output_is_valid_json_lines_with_unicode(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    c = b.add_chat(fx.writer, CHAT, 45, handles=[PHONE])
    b.add_message(fx.writer, c, text="emoji \U0001f600 and \"quotes\"\nnewline", handle=PHONE)
    out = io.StringIO()
    assert main(["tail", "--since-rowid", "0", "--db", str(fx.path)], stdout=out) == EXIT_OK
    raw = out.getvalue()
    assert raw.count("\n") == 1  # one line per message even with embedded newlines
    assert json.loads(raw)["text"] == "emoji \U0001f600 and \"quotes\"\nnewline"
    assert "\U0001f600" in raw  # ensure_ascii=False


def test_tail_follow_streams_new_rows_until_interrupted(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    c = b.add_chat(fx.writer, CHAT, 45, handles=[PHONE])
    m1 = b.add_message(fx.writer, c, text="before", handle=PHONE)
    rounds: list[float] = []

    def fake_sleep(seconds: float) -> None:
        rounds.append(seconds)
        if len(rounds) == 1:
            b.add_message(fx.writer, c, text="during", handle=PHONE)
            return
        raise KeyboardInterrupt

    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["tail", "--follow", "--interval", "0.25", "--db", str(fx.path)],
        stdout=out,
        stderr=err,
        sleep=fake_sleep,
    )
    assert code == EXIT_INTERRUPTED
    assert rounds == [0.25, 0.25]
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    # Started at now: "before" (m1) is not replayed; "during" is streamed.
    assert [m["text"] for m in lines] == ["during"]
    assert lines[0]["rowid"] == m1 + 1
    assert err.getvalue() == ""


def test_tail_follow_replays_from_since_rowid(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    c = b.add_chat(fx.writer, CHAT, 45, handles=[PHONE])
    b.add_message(fx.writer, c, text="one", handle=PHONE)
    b.add_message(fx.writer, c, text="two", handle=PHONE)

    def fake_sleep(_: float) -> None:
        raise KeyboardInterrupt

    out = io.StringIO()
    main(["tail", "--follow", "--since-rowid", "0", "--db", str(fx.path)], stdout=out,
         sleep=fake_sleep)
    assert [json.loads(line)["text"] for line in out.getvalue().splitlines()] == ["one", "two"]


def test_tail_cursor_ahead_restarts_at_now(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    c = b.add_chat(fx.writer, CHAT, 45, handles=[PHONE])
    b.add_message(fx.writer, c, text="one", handle=PHONE)
    code, lines, err = run(["tail", "--since-rowid", "999", "--db", str(fx.path)])
    assert code == EXIT_OK
    assert lines == []
    assert "ahead of MAX(ROWID)" in err


# ---------------------------------------------------------------------------
# chats
# ---------------------------------------------------------------------------


def test_chats_lists_summaries_newest_first(fixture_db: FixtureDB) -> None:
    ids = populate(fixture_db)
    code, lines, _ = run(["chats", "--db", str(fixture_db.path)])
    assert code == EXIT_OK
    assert [c["guid"] for c in lines] == [GROUP, CHAT]
    group = lines[0]
    assert group["rowid"] == ids["c2"]
    assert group["is_group"] is True
    assert group["display_name"] == "Synthetic Group"
    assert group["chat_name"] == "Synthetic Group"
    assert group["last_rowid"] == ids["m4"]
    assert isinstance(group["last_date"], float)
    assert lines[1]["chat_identifier"] == PHONE
    assert lines[1]["is_group"] is False


def test_chats_limit(fixture_db: FixtureDB) -> None:
    populate(fixture_db)
    _, lines, _ = run(["chats", "--limit", "1", "--db", str(fixture_db.path)])
    assert [c["guid"] for c in lines] == [GROUP]


def test_chats_excludes_empty_chats(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    b.add_chat(fx.writer, CHAT, 45, handles=[PHONE])
    _, lines, _ = run(["chats", "--db", str(fx.path)])
    assert lines == []


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_prints_hits_newest_first(fixture_db: FixtureDB) -> None:
    ids = populate(fixture_db)
    code, lines, _ = run(["search", "HELLO", "--db", str(fixture_db.path)])
    assert code == EXIT_OK
    assert [h["rowid"] for h in lines] == [ids["m3"], ids["m1"]]
    hit = lines[1]
    assert list(hit) == [
        "rowid", "chat_rowid", "chat_guid", "chat_identifier", "chat_display_name",
        "chat_name", "is_group", "date", "is_from_me", "sender_handle", "text", "match_index",
    ]  # fmt: skip
    assert hit["text"] == "hello from fixture"
    assert hit["match_index"] == 0
    assert hit["sender_handle"] == PHONE
    assert lines[0]["text"] == "blob only hello"
    assert lines[0]["match_index"] == len("blob only ")


def test_search_limit_and_no_hits(fixture_db: FixtureDB) -> None:
    populate(fixture_db)
    _, lines, _ = run(["search", "hello", "--limit", "1", "--db", str(fixture_db.path)])
    assert len(lines) == 1
    _, lines, _ = run(["search", "zzz-nothing", "--db", str(fixture_db.path)])
    assert lines == []


# ---------------------------------------------------------------------------
# broken pipe: ``python -m imessage_chatdb tail ... | head -1``
# ---------------------------------------------------------------------------


class _ReaderGoneAfterFirstLine(io.StringIO):
    """A stdout whose reader closed the pipe after the first line (what ``head -1`` does)."""

    def __init__(self) -> None:
        super().__init__()
        self.lines = 0
        self.failed_writes = 0

    def write(self, s: str) -> int:
        if self.lines >= 1:
            self.failed_writes += 1
            raise BrokenPipeError(32, "Broken pipe")
        n = super().write(s)
        if s.endswith("\n"):
            self.lines += 1
        return n


def test_broken_stdout_returns_0_without_raising(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    c = b.add_chat(fx.writer, CHAT, 45, handles=[PHONE])
    for i in range(5):
        b.add_message(fx.writer, c, text=f"line {i}", handle=PHONE)
    fd1_before = os.fstat(1)
    out, err = _ReaderGoneAfterFirstLine(), io.StringIO()
    code = main(["tail", "--since-rowid", "0", "--db", str(fx.path)], stdout=out, stderr=err)
    assert code == EXIT_OK
    assert out.failed_writes >= 1  # the pipe really broke mid-stream
    assert [json.loads(line)["text"] for line in out.getvalue().splitlines()] == ["line 0"]
    assert err.getvalue() == ""
    # an injected stream without a descriptor never redirects the process's real stdout
    fd1_after = os.fstat(1)
    assert (fd1_after.st_dev, fd1_after.st_ino) == (fd1_before.st_dev, fd1_before.st_ino)


def test_broken_stdout_in_follow_mode_returns_0(make_db: MakeDB) -> None:
    fx = make_db("macos27")
    c = b.add_chat(fx.writer, CHAT, 45, handles=[PHONE])
    for i in range(3):
        b.add_message(fx.writer, c, text=f"line {i}", handle=PHONE)

    def never_called(_: float) -> None:
        raise AssertionError("the pipe broke during the first round; no sleep expected")

    out = _ReaderGoneAfterFirstLine()
    code = main(
        ["tail", "--follow", "--since-rowid", "0", "--db", str(fx.path)],
        stdout=out,
        stderr=io.StringIO(),
        sleep=never_called,
    )
    assert code == EXIT_OK
    assert out.lines == 1 and out.failed_writes >= 1


def test_broken_pipe_in_a_subprocess_exits_0_quietly(make_db: MakeDB) -> None:
    """At the OS level: exit 0 and an empty stderr (no "Exception ignored ... BrokenPipeError")."""
    import subprocess

    fx = make_db("macos27")
    w = fx.writer
    c = b.add_chat(w, CHAT, 45, handles=[PHONE])
    w.execute("BEGIN")
    first = b.add_message(w, c, text="x" * 300, handle=PHONE)
    for _ in range(1500):  # far more than any pipe buffer: the writer must block, then get EPIPE
        b.add_message(w, c, text="x" * 300, handle=PHONE)
    w.execute("COMMIT")

    argv = ["tail", "--since-rowid", "0", "--db", str(fx.path)]
    proc = subprocess.Popen(
        [sys.executable, "-m", "imessage_chatdb", *argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None and proc.stderr is not None
    line = proc.stdout.readline()
    proc.stdout.close()  # the reader goes away, exactly as ``| head -1`` does
    err = proc.stderr.read()
    code = proc.wait(timeout=60)
    assert json.loads(line)["rowid"] == first
    assert code == EXIT_OK, err
    assert err == ""


# ---------------------------------------------------------------------------
# streams
# ---------------------------------------------------------------------------


def test_main_defaults_to_sys_stdout(
    fixture_db: FixtureDB, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(fixture_db)
    assert main(["chats", "--db", str(fixture_db.path)]) == EXIT_OK
    captured = capsys.readouterr()
    assert captured.err == ""
    assert [json.loads(line)["guid"] for line in captured.out.splitlines()] == [GROUP, CHAT]


def test_module_is_runnable_as_script(fixture_db: FixtureDB) -> None:
    """``python -m imessage_chatdb`` resolves to this module's ``main``."""
    import subprocess

    populate(fixture_db)
    proc = subprocess.run(
        [sys.executable, "-m", "imessage_chatdb", "check", "--db", str(fixture_db.path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == EXIT_OK, proc.stderr
    assert json.loads(proc.stdout)["profile"] == fixture_db.profile


# ---------------------------------------------------------------------------
# suite-wide read-only guard: every subcommand over the pristine database
# ---------------------------------------------------------------------------


def test_every_subcommand_leaves_the_pristine_file_untouched(pristine_db: Pristine) -> None:
    db = str(pristine_db.path)
    code, lines, err = run(["check", "--db", db])
    assert code == EXIT_OK and err == ""
    assert lines[0]["profile"] == "macos27" and lines[0]["max_rowid"] == pristine_db.rowids[-1]
    code, lines, _ = run(["tail", "--since-rowid", "0", "--db", db])
    assert code == EXIT_OK and [m["rowid"] for m in lines] == list(pristine_db.rowids)
    code, lines, _ = run(["chats", "--db", db])
    assert code == EXIT_OK and len(lines) == 2
    code, lines, _ = run(["search", "synthetic", "--limit", "2", "--db", db])
    assert code == EXIT_OK and len(lines) == 2
    assert sha256_of(pristine_db.path) == pristine_db.sha256
