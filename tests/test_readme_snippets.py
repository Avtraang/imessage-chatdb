"""Keep the README honest (DESIGN.md 11 T9).

Every ```python block in README.md is compiled.  Blocks whose first line is
``# doctest-fixture`` are also executed, each in a fresh namespace with
``DB_PATH`` bound to a synthetic database built under ``tmp_path`` (covering
chats, blob-only text, tapbacks, a reply, a URL balloon with an embedded image,
a real file attachment, a plugin payload and an edit).  Snippets print; pytest
captures.  The real database is never touched.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from imessage_chatdb.link_preview import LINK_BALLOON
from tests.conftest import FixtureDB, MakeDB
from tests.fixtures import builders as b
from tests.fixtures.keyed_archive_writer import make_link_payload
from tests.fixtures.typedstream_writer import encode_attributed_body

README = Path(__file__).resolve().parents[1] / "README.md"
FIXTURE_MARK = "# doctest-fixture"

PHONE = "+15550001234"
OTHER = "+15550009876"
EMAIL = "test@example.invalid"
CHAT = "any;-;+15550001234"
GROUP = "any;+;chat9001"


def python_blocks(text: str) -> list[str]:
    """Source of every fenced ```python block, in document order."""
    return [m.group(1) for m in re.finditer(r"```python\n(.*?)```", text, re.DOTALL)]


BLOCKS = python_blocks(README.read_text(encoding="utf-8"))
FIXTURE_BLOCKS = [(i, src) for i, src in enumerate(BLOCKS) if src.startswith(FIXTURE_MARK)]


def build_readme_db(fx: FixtureDB, tmp_path: Path) -> None:
    """A small but feature-complete synthetic database for the snippets."""
    w = fx.writer
    one = b.add_chat(w, CHAT, 45, handles=[PHONE])
    grp = b.add_chat(w, GROUP, 43, display_name="Synthetic Group", handles=[PHONE, OTHER, EMAIL])

    m1 = b.add_message(w, one, text="hello from the fixture", handle=PHONE)
    b.add_message(w, one, text="hello back", is_from_me=1)
    b.add_message(w, one, body=encode_attributed_body("blob-only hello ￼"), handle=PHONE)
    b.add_message(w, one, assoc_guid=f"p:0/SYN-MSG-{m1:06d}", assoc_type=2000, handle=PHONE)
    b.add_message(
        w, one, assoc_guid=f"p:0/SYN-MSG-{m1:06d}", assoc_type=2006, assoc_emoji="\U0001f525",
        handle=PHONE,
    )
    b.add_message(w, one, text="a reply", reply_to=f"SYN-MSG-{m1:06d}", handle=PHONE)

    # A real file attachment that exists on disk (under tmp_path).
    photo = tmp_path / "IMG_0001.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)
    m_att = b.add_message(w, one, text="￼", has_att=1, handle=PHONE)
    b.add_attachment(w, m_att, "ATT-1", "image/jpeg", "IMG_0001.jpg", str(photo))

    # A URL balloon with an embedded hero image and its plugin payload row.
    payload = make_link_payload(
        "https://example.invalid/page", "Example page", "A summary", "example.invalid",
        embed="png",
    )
    m_link = b.add_message(
        w, grp, text="https://example.invalid/page", balloon=LINK_BALLOON, payload=payload,
        has_att=1, handle=OTHER,
    )
    b.add_attachment(
        w, m_link, "ATT-2", None, "abc.pluginPayloadAttachment",
        "~/synthetic/Attachments/ab/abc.pluginPayloadAttachment", hide=1,
    )

    m_edit = b.add_message(w, grp, text="group text, edited", handle=PHONE)
    b.set_date_edited(w, m_edit, b.BASE_DATE_NS + 10_000 * b.DATE_STEP_NS)
    b.add_message(w, grp, text="latest group message", is_from_me=1, service="SMS")


@pytest.fixture
def readme_db(make_db: MakeDB, tmp_path: Path) -> FixtureDB:
    fx = make_db("macos27", name="readme.db")
    build_readme_db(fx, tmp_path)
    return fx


def test_readme_has_fixture_snippets() -> None:
    assert len(BLOCKS) >= 10
    assert len(FIXTURE_BLOCKS) >= 8


@pytest.mark.parametrize("index", range(len(BLOCKS)))
def test_every_python_block_compiles(index: int) -> None:
    compile(BLOCKS[index], f"README.md#python-block-{index}", "exec")


@pytest.mark.parametrize(
    ("index", "source"), FIXTURE_BLOCKS, ids=[str(i) for i, _ in FIXTURE_BLOCKS]
)
def test_fixture_snippet_runs(
    index: int, source: str, readme_db: FixtureDB, capsys: pytest.CaptureFixture[str]
) -> None:
    namespace: dict[str, object] = {
        "__name__": f"readme_snippet_{index}",
        "DB_PATH": str(readme_db.path),
    }
    code = compile(source, f"README.md#python-block-{index}", "exec")
    exec(code, namespace)  # noqa: S102 - the README is ours
    out = capsys.readouterr().out
    assert out.strip() != "", "a runnable README snippet should print something"
    assert "Traceback" not in out


def test_snippets_never_reach_outside_tmp_path(readme_db: FixtureDB) -> None:
    """The snippets only know DB_PATH, and DB_PATH is under tmp_path."""
    home_library = (Path.home() / "Library").resolve()
    assert not readme_db.path.resolve().is_relative_to(home_library)
    for _, source in FIXTURE_BLOCKS:
        assert "~/Library" not in source
        assert "imessage_chatdb.open()" not in source  # would hit the default path
