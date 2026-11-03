"""Reading a PDF with read_file: its text page by page, filled-in form values, and one page drawn as a picture.

A file is a PDF by its ``%PDF-`` header, never its name. PDFium is not thread-safe, so every call into it
holds one lock. The blocking work (parsing, drawing, the tray write) runs off the event loop. The drawn page
goes to the reader's working tray under its content hash and returns with the page text in one
``MediaToolResult``, the same pattern as a marked document region (``tools/document_regions.py``).
"""

from __future__ import annotations

import asyncio
import ctypes
import io
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from infra.db_async import run_in_db

log = logging.getLogger(__name__)

HEADER = b"%PDF-"
PAGE_LONG_EDGE = 1568
SHAPES_THAT_MAKE_A_DRAWING = 100

READ_FILE_NOTE = (
    "A PDF reads as its text, page by page; a form's filled-in values are listed apart from their labels. "
    "Use page: N to see one page as drawn (a picture plus its text) for a scan (its page says it has no text), "
    "a chart or picture (its page says so), a form, or any table whose numbers matter, since empty cells can "
    "shift numbers into the wrong column."
)
PAGE_PARAMETER = {
    "type": "integer", "minimum": 1,
    "description": "For a PDF: show this one page as a picture, along with its text.",
}
LOCKED = (
    "This PDF needs a password to open, so it can't be read here. "
    "Tell the person, and ask them for a copy without a password."
)
DAMAGED = "This PDF is damaged or unreadable."
NO_TEXT = "--- Page {n} --- (no text: a scan or a picture. Read it with page: {n} to see it.)"
NO_TEXT_BLIND = "--- Page {n} --- (no text: a scan or a picture, and this model can't view pictures.)"
DRAWN = "--- Page {n} --- (has pictures or drawings the text below misses. Read it with page: {n} to see them.)"
DRAWN_BLIND = "--- Page {n} --- (has pictures or drawings the text below misses, and this model can't view pictures.)"
FORMS = "Filled-in values on page {n} (their labels are on the page; page: {n} shows them in place):"
NO_SUCH_PAGE = "Error: this PDF has {count} page(s); page must be from 1 to {count}."
NO_PICTURE = "Error: the picture of page {n} could not be saved ({reason})."

_PDFIUM = threading.Lock()


class PdfRefused(Exception):
    """A PDF that cannot be read, with the message the agent gets."""


def is_pdf(data: bytes) -> bool:
    return data.startswith(HEADER)


@contextmanager
def _opened(data: bytes) -> Iterator[Any]:
    """The document with its form layer ready, closed afterwards; PDFium's own refusals become messages."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise PdfRefused(LOCKED if exc.err_code == raw.FPDF_ERR_PASSWORD else DAMAGED) from exc
    try:
        pdf.init_forms()
        yield pdf
    finally:
        pdf.close()


def _has_pictures_or_drawings(page: Any) -> bool:
    """An embedded image, or ``SHAPES_THAT_MAKE_A_DRAWING`` path objects (also inside form XObjects)."""
    import pypdfium2.raw as raw

    shapes = 0
    for obj in page.get_objects(max_depth=3):
        if obj.type == raw.FPDF_PAGEOBJ_IMAGE:
            return True
        if obj.type == raw.FPDF_PAGEOBJ_PATH:
            shapes += 1
            if shapes >= SHAPES_THAT_MAKE_A_DRAWING:
                return True
    return False


def _field_values(pdf: Any, page: Any) -> list[str]:
    """The filled-in form values on a page, in field order."""
    import pypdfium2.raw as raw

    if pdf.formenv is None:
        return []
    values = []
    for index in range(raw.FPDFPage_GetAnnotCount(page.raw)):
        annot = raw.FPDFPage_GetAnnot(page.raw, index)
        try:
            if raw.FPDFAnnot_GetSubtype(annot) != raw.FPDF_ANNOT_WIDGET:
                continue
            size = raw.FPDFAnnot_GetFormFieldValue(pdf.formenv.raw, annot, None, 0)
            buffer = (ctypes.c_ushort * size)()
            raw.FPDFAnnot_GetFormFieldValue(pdf.formenv.raw, annot, buffer, size)
            value = bytes(buffer).decode("utf-16le", errors="replace").rstrip("\x00").strip()
        finally:
            raw.FPDFPage_CloseAnnot(annot)
        if value and value != "Off":
            values.append(value)
    return values


def _text(page: Any) -> str:
    """The page's own text (not what lies off the page), lines split by ``\\n``, a line-end hyphen's marker dropped."""
    text = page.get_textpage().get_text_bounded().replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\x02", "").strip()


def _lines(number: int, text: str, values: list[str], head: str) -> list[str]:
    lines = [head, *([text] if text else [])]
    if values:
        lines += [FORMS.format(n=number), *values]
    return lines


def read_text(data: bytes, can_view_images: bool) -> str:
    """The whole PDF as text: the page count, then each page with its notes and form values."""
    with _PDFIUM, _opened(data) as pdf:
        count = len(pdf)
        lines = [f"{count} page{'' if count == 1 else 's'}"]
        for number in range(1, count + 1):
            page = pdf[number - 1]
            try:
                text = _text(page)
                if not text:
                    head = (NO_TEXT if can_view_images else NO_TEXT_BLIND).format(n=number)
                elif _has_pictures_or_drawings(page):
                    head = (DRAWN if can_view_images else DRAWN_BLIND).format(n=number)
                else:
                    head = f"--- Page {number} ---"
                lines += _lines(number, text, _field_values(pdf, page), head)
            finally:
                page.close()
    return "\n".join(lines)


def draw_page(data: bytes, number: int) -> tuple[str, bytes]:
    """One page's text (with its form values) and the page as a PNG, forms drawn in place."""
    with _PDFIUM, _opened(data) as pdf:
        count = len(pdf)
        if not 1 <= number <= count:
            raise PdfRefused(NO_SUCH_PAGE.format(count=count))
        page = pdf[number - 1]
        try:
            width, height = page.get_size()
            image = page.render(scale=PAGE_LONG_EDGE / max(width, height), may_draw_forms=True).to_pil()
            png = io.BytesIO()
            image.save(png, format="PNG")
            text = "\n".join(_lines(number, _text(page), _field_values(pdf, page), f"--- Page {number} ---"))
        finally:
            page.close()
    return text, png.getvalue()


def _images_accepted(thread_id: str | None, turn_id: str | None) -> bool:
    """Whether the turn's model takes images, from the catalog row the media delivery boundary also reads.

    The running turn's model, else the thread's; unknown reads as able, so no limit is claimed that isn't known.
    """
    from infra.database import get_db
    from llm.resolution import ModelUnavailableError, resolve_execution_target

    db = get_db()
    try:
        row = None
        if turn_id:
            row = db.execute("SELECT model FROM active_turns WHERE id=? AND thread_id=?", (turn_id, thread_id)).fetchone()
        row = row or db.execute("SELECT model FROM threads WHERE id=?", (thread_id,)).fetchone()
        return resolve_execution_target(row["model"] if row else None, db=db).supports_vision
    except ModelUnavailableError:
        return True
    finally:
        db.close()


def _save_png(thread_id: str, png: bytes, label: str) -> Any:
    from doceditor import image_regions
    from infra.database import get_db

    db = get_db()
    try:
        return image_regions.write_tray_png(db, png, reader_thread_id=thread_id, label=label)
    finally:
        db.close()


async def read_pdf(
    data: bytes, path: Path, offset: int | None, limit: int | None, page: int | None, kwargs: dict[str, Any],
    render_lines: Callable[[str, int | None, int | None], tuple[str, bool]],
) -> str:
    """What read_file returns for a PDF: its numbered text, or with ``page`` that page's text and picture."""
    from core.media_reads import IMAGE_KIND, MediaReadReference, MediaToolResult
    from infra import state

    thread_id = kwargs.get("_thread_id")
    try:
        if page is None:
            identity = state._current_turn_identity.get(None)
            can_view = await run_in_db(_images_accepted, thread_id, getattr(identity, "turn_id", None))
            return render_lines(await asyncio.to_thread(read_text, data, can_view), offset, limit)[0]
        text, png = await asyncio.to_thread(draw_page, data, page)
    except PdfRefused as refused:
        return str(refused)
    except Exception:  # noqa: BLE001 - a hostile or broken file must not fail the turn
        log.exception("PDF read failed")
        return DAMAGED
    try:
        marked = await run_in_db(_save_png, thread_id, png, f"{path.stem[:60]}-page-{page}")
    except (OSError, ValueError) as exc:
        return f"{text}\n{NO_PICTURE.format(n=page, reason=exc)}"
    reference = MediaReadReference(
        path=str(marked.path), mime_type="image/png", kind=IMAGE_KIND,
        byte_size=marked.byte_size, sha256=marked.sha256,
    )
    return MediaToolResult(f"{text}\n{reference.placeholder_text()}", reference)
