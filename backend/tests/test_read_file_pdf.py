"""read_file opens PDFs: text page by page, page notes, form values, one page as a picture, locked and damaged files."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from doc_feedback_helpers import MODEL, seed_thread
from infra.database import get_db
from pdf_fixtures import LOCKED_PDF, Page, build, text_pdf
from tools import pdf_read
from tools.filesystem_rw import ReadFileTool

THREAD = "reader"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """An isolated sandbox-allowed workspace root, as the other read_file tests use."""
    from tools import sandbox_config

    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [root])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(sandbox_config, "_list_authoritative_repo_policies", lambda: [])
    return root


@pytest.fixture
def thread(isolated_db):
    """A thread whose model takes images unless a test says otherwise."""
    db = get_db()
    try:
        seed_thread(db, THREAD)
        db.execute("UPDATE models SET supports_vision=1 WHERE id=?", (MODEL,))
        db.commit()
    finally:
        db.close()
    return THREAD


def set_vision(model: str, value: int) -> None:
    db = get_db()
    try:
        db.execute("UPDATE models SET supports_vision=? WHERE id=?", (value, model))
        db.commit()
    finally:
        db.close()


async def read(root, data: bytes, name: str = "doc.pdf", **kwargs):
    (root / name).write_bytes(data)
    return await ReadFileTool(workspace=root).execute(path=name, _thread_id=THREAD, **kwargs)


def body(result: str) -> list[str]:
    """The lines of a numbered read, without their line numbers."""
    return [line.split("\t", 1)[1] if "\t" in line else line for line in str(result).splitlines()]


@pytest.mark.asyncio
async def test_a_text_pdf_reads_as_its_page_count_then_each_page(workspace, thread):
    result = await read(workspace, text_pdf("First page text", "Second page text"))
    assert body(result) == ["2 pages", "--- Page 1 ---", "First page text", "--- Page 2 ---", "Second page text"]
    assert (await read(workspace, text_pdf("Only"), "one.pdf")).startswith("     1\t1 page\n")


@pytest.mark.asyncio
async def test_a_pdf_is_recognised_by_its_header_not_its_name(workspace, thread):
    assert body(await read(workspace, text_pdf("Inside"), "no-extension"))[0] == "1 page"
    assert body(await read(workspace, b"plain words\nsecond line\n", "notes.pdf")) == ["plain words", "second line"]


@pytest.mark.asyncio
async def test_a_page_without_text_says_so(workspace, thread):
    result = await read(workspace, build([Page(text="Cover"), Page(image=True), Page()]))
    assert body(result) == [
        "3 pages", "--- Page 1 ---", "Cover",
        "--- Page 2 --- (no text: a scan or a picture. Read it with page: 2 to see it.)",
        "--- Page 3 --- (no text: a scan or a picture. Read it with page: 3 to see it.)",
    ]


@pytest.mark.asyncio
async def test_a_page_with_a_picture_or_a_hundred_shapes_says_so_and_still_gives_its_text(workspace, thread):
    pages = [Page(text="Photo", image=True), Page(text="Chart", shapes=100), Page(text="Table", shapes=99)]
    note = "(has pictures or drawings the text below misses. Read it with page: {n} to see them.)"
    assert body(await read(workspace, build(pages))) == [
        "3 pages",
        "--- Page 1 --- " + note.format(n=1), "Photo",
        "--- Page 2 --- " + note.format(n=2), "Chart",
        "--- Page 3 ---", "Table",
    ]


@pytest.mark.asyncio
async def test_a_model_that_cannot_view_pictures_gets_the_variant_notes(workspace, thread):
    set_vision(MODEL, 0)
    pages = [Page(text="Photo", image=True), Page(image=True)]
    assert body(await read(workspace, build([pages[1], pages[0]]))) == [
        "2 pages",
        "--- Page 1 --- (no text: a scan or a picture, and this model can't view pictures.)",
        "--- Page 2 --- (has pictures or drawings the text below misses, and this model can't view pictures.)",
        "Photo",
    ]


def test_a_line_end_hyphen_marker_from_the_reader_is_dropped_so_the_word_joins():
    from types import SimpleNamespace

    textpage = SimpleNamespace(get_text_bounded=lambda: "language representa\x02tion\r\nmodel\r\n")
    assert pdf_read._text(SimpleNamespace(get_textpage=lambda: textpage)) == "language representation\nmodel"


def test_the_running_turns_model_decides_before_the_threads(isolated_db):
    db = get_db()
    try:
        seed_thread(db, THREAD)
        db.execute("UPDATE models SET supports_vision=1 WHERE id=?", (MODEL,))
        db.execute("UPDATE models SET supports_vision=0 WHERE id='opus'")
        db.execute(
            "INSERT INTO active_turns(id, thread_id, execution_epoch, model, started_at, boot_epoch) "
            "VALUES ('turn-1', ?, 0, 'opus', '2026-11-02T00:00:00+00:00', 'boot')", (THREAD,),
        )
        db.commit()
    finally:
        db.close()
    assert pdf_read._images_accepted(THREAD, None) is True
    assert pdf_read._images_accepted(THREAD, "turn-1") is False
    assert pdf_read._images_accepted(None, None) is True


@pytest.mark.asyncio
async def test_filled_in_form_values_follow_their_page_in_field_order(workspace, thread):
    result = await read(workspace, build([Page(text="Name and age", fields=("Alice Smith", "42")), Page(text="Notes")]))
    assert body(result) == [
        "2 pages", "--- Page 1 ---", "Name and age",
        "Filled-in values on page 1 (their labels are on the page; page: 1 shows them in place):",
        "Alice Smith", "42",
        "--- Page 2 ---", "Notes",
    ]


@pytest.mark.asyncio
async def test_the_character_cap_and_line_window_apply_to_a_pdfs_text(workspace, thread):
    window = await read(workspace, text_pdf("a", "b", "c"), offset=3, limit=2)
    assert body(window)[:2] == ["a", "--- Page 2 ---"] and "Use offset=5 to continue" in window
    long_text = await read(workspace, text_pdf(*["word " * 9] * 700), "long.pdf")
    assert "output truncated at 30,000 chars" in long_text


@pytest.mark.asyncio
async def test_page_n_returns_its_text_and_the_page_drawn_through_the_image_path(workspace, thread):
    from core.media_reads import MediaToolResult

    pdf = build([Page(text="Cover"), Page(text="Name:", fields=("Alice Smith",))])
    result = await read(workspace, pdf, page=2)
    assert isinstance(result, MediaToolResult) and result.media.kind == "image"
    lines = str(result).splitlines()
    assert lines[:5] == [
        "--- Page 2 ---", "Name:",
        "Filled-in values on page 2 (their labels are on the page; page: 2 shows them in place):",
        "Alice Smith", result.media.placeholder_text(),
    ]
    drawn = result.media.path
    assert "/working/" in drawn and drawn.endswith(".png") and result.media.sha256[:16] in drawn
    with Image.open(drawn) as image:
        assert image.format == "PNG" and image.width == 1568 and abs(image.height - 1045) <= 1
    again = await read(workspace, pdf, page=2)
    assert again.media.path == drawn
    assert (await read(workspace, pdf, page=1)).media.path != drawn


@pytest.mark.asyncio
async def test_a_form_value_is_drawn_in_place_on_the_page_picture(workspace, thread):
    def drawn(fields):
        return read(workspace, build([Page(text="Name:", fields=fields)]), page=1)

    with_value, without = await drawn(("Alice Smith",)), await drawn(())
    pixels = lambda result: Image.open(result.media.path).convert("L").tobytes()  # noqa: E731
    assert pixels(with_value) != pixels(without)


@pytest.mark.asyncio
async def test_a_page_that_is_not_in_the_pdf_says_how_many_there_are(workspace, thread):
    assert await read(workspace, text_pdf("a", "b"), page=3) == "Error: this PDF has 2 page(s); page must be from 1 to 2."


@pytest.mark.asyncio
async def test_a_locked_pdf_and_a_damaged_pdf_get_their_own_messages(workspace, thread):
    locked = "This PDF needs a password to open, so it can't be read here. Tell the person, and ask them for a copy without a password."
    assert await read(workspace, LOCKED_PDF, "locked.pdf") == locked
    assert await read(workspace, LOCKED_PDF, "locked.pdf", page=1) == locked
    assert await read(workspace, b"%PDF-1.4 this is not a pdf", "bad.pdf") == "This PDF is damaged or unreadable."
    assert await read(workspace, b"%PDF-1.4 this is not a pdf", "bad.pdf", page=1) == "This PDF is damaged or unreadable."


@pytest.mark.asyncio
async def test_a_file_that_is_not_a_pdf_reads_as_before_and_ignores_page(workspace, thread):
    text = "alpha\nbeta\n"
    assert await read(workspace, text.encode(), "a.txt") == "     1\talpha\n     2\tbeta"
    assert await read(workspace, text.encode(), "a.txt", page=2) == "     1\talpha\n     2\tbeta"


def test_the_tool_text_is_what_the_owner_approved():
    tool = ReadFileTool()
    assert (
        "Output is capped at ~30K characters. For large files, use offset/limit to read sections. "
        "A PDF reads as its text, page by page; a form's filled-in values are listed apart from their labels. "
        "Use page: N to see one page as drawn (a picture plus its text) for a scan (its page says it has no text), "
        "a chart or picture (its page says so), a form, or any table whose numbers matter, since empty cells can "
        "shift numbers into the wrong column.\n\n"
    ) in tool.description
    assert tool.parameters["properties"]["page"] == {
        "type": "integer", "minimum": 1,
        "description": "For a PDF: show this one page as a picture, along with its text.",
    }
    assert tool.parameters["required"] == ["path"]
