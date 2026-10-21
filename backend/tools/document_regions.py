"""Region-comment schema and rendered image tool results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.media_reads import IMAGE_KIND, MediaReadReference, MediaToolResult
from doceditor import image_regions
from infra.database import get_db
from infra.db_async import run_in_db


REGION_SCHEMA = {
    "type": ["object", "null"],
    "properties": {"x": {"type": "number"}, "y": {"type": "number"},
                   "w": {"type": "number"}, "h": {"type": "number"}},
    "required": ["x", "y"],
    "description": ("A box {x, y, w, h} (or point {x, y}) in fractions 0..1 of an image, origin top-left: instead of "
                    "quote on an image artifact; with quote naming the image (its ![alt](path) or path; near picks "
                    "among repeats) in a document. The result shows your box drawn, to move with doc_comment_update."),
}


async def region_result(
    payload: dict[str, Any], row: dict[str, Any], path: str | None, reader: str,
) -> str | MediaToolResult:
    """The JSON result plus the image with this comment's numbered box drawn (DCOL-FR13.7)."""
    def _render():
        name = Path(str(image_regions.image_of(row, path))).stem
        db = get_db()
        try:
            image = image_regions.comment_image(db, row, path, image_regions.reader_bases(db, reader))
            label = f"{name}-region-{row['region_number']}"
            return image_regions.write_marked_image(
                db, image=image_regions.require_image(image), rows=[row], reader_thread_id=reader, label=label)
        finally:
            db.close()

    try:
        marked = await run_in_db(_render)
    except image_regions.RENDER_ERRORS as exc:  # a scope refusal (CommentActionError) is a ValueError too
        return json.dumps({**payload, "marked_image_error": str(exc)}, sort_keys=True)
    reference = MediaReadReference(
        path=str(marked.path), mime_type="image/png", kind=IMAGE_KIND,
        byte_size=marked.byte_size, sha256=marked.sha256,
    )
    text = json.dumps({**payload, "marked_image": str(marked.path)}, sort_keys=True)
    return MediaToolResult(f"{text}\n{reference.placeholder_text()}", reference)
