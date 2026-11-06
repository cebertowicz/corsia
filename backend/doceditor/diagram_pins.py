"""Pins on a rendered mermaid diagram, placed like pins on an image (TDC-FR7 to FR9).

A diagram has no file, so its pin is a text comment anchored on the block's opening fence line
(```` ```mermaid ````), which edits to the diagram leave alone, as an image's markdown is left alone when its
file changes. The revision a pin is placed on is the SHA-256 of the block's source then; the pin is stale when the
block that opens at its anchor now differs or is gone (the images' "changed since this comment" state), and the
agent is told which block it is on.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping

from markdown_it import MarkdownIt

from doceditor.doc_text import read_doc_text
from doceditor.quote_anchor import _cp_at_u16


#: How a delivery line for a diagram opens; ``plans.feedback_nodes`` puts these lines last, because they carry source.
LINE_PREFIX = "Diagram"


def _blocks(text: str) -> dict[int, tuple[int, int, str]]:
    return {
        token.map[0]: (token.map[0] + 1, token.map[1], token.content.removesuffix("\n"))
        for token in MarkdownIt().parse(text)
        if token.type == "fence" and token.map and token.info.strip().lower() == "mermaid"
    }


def _anchor_line(text: str, from16: int | None) -> int | None:
    start = _cp_at_u16(text, from16)
    if start is None or (start and text[start - 1] != "\n"):
        return None
    return text.count("\n", 0, start)


def block_at(text: str, from16: int | None) -> tuple[int, int, str] | None:
    """The block whose opening line starts at the UTF-16 anchor offset."""
    line = _anchor_line(text, from16)
    return _blocks(text).get(line) if line is not None else None


def source_revision(source: str) -> str:
    return hashlib.sha256(source.strip().encode()).hexdigest()


def revision(text: str, from16: int | None) -> str | None:
    """The revision a pin is placed on: the hash of the block's source as it is now."""
    block = block_at(text, from16)
    return source_revision(block[2]) if block else None


def is_diagram_group(group: list[dict], path: str | None) -> bool:
    """Region comments with no image file, in a document that is not an image: pins on its diagrams."""
    return bool(group) and not group[0].get("image_src") and bool(path)


def mark_stale(group: Iterable[dict], path: str | None) -> None:
    """A document that cannot be read leaves its pins unmarked rather than calling them all stale."""
    try:
        text = read_doc_text(path)
    except (OSError, ValueError):
        return
    blocks = _blocks(text)
    revisions: dict[int | None, str | None] = {}
    for row in group:
        offset = row.get("from_off")
        if offset not in revisions:
            line = _anchor_line(text, offset)
            block = blocks.get(line) if line is not None else None
            revisions[offset] = source_revision(block[2]) if block else None
        # No mermaid block opens at the anchor any more (deleted, or its fence no longer says mermaid): changed.
        row["region_stale"] = row.get("image_rev") != revisions[offset]


def delivery_line(row: Mapping[str, Any], path: str | None) -> str:
    """The wake line that names the diagram a pin is on: where its block is, and its source."""
    name = (path or "the document").rsplit("/", 1)[-1]
    try:
        block = block_at(read_doc_text(path), row.get("from_off"))
    except (OSError, ValueError):
        return f"{LINE_PREFIX}: the document could not be read, so this diagram's source is not shown."
    if block is None:
        return f"{LINE_PREFIX}: the diagram is no longer where this comment was placed (no mermaid block starts there; it was deleted or changed)."
    first, last, source = block
    return f"{LINE_PREFIX} (mermaid block, lines {first} to {last} of {name}):\n```mermaid\n{source}\n```"
