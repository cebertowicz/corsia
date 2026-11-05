"""Pins on a rendered mermaid diagram, placed like pins on an image (TDC-FR7 to FR9).

A diagram has no file, so its pin is a text comment anchored on the block's opening fence line
(```` ```mermaid ````), which edits to the diagram leave alone, as an image's markdown is left alone when its
file changes. The revision a pin is placed on is the SHA-256 of the block's source then; the pin is stale when the
block that opens at its anchor now differs (the images' "changed since this comment" state), and the agent is
told which block it is on.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Mapping

from doceditor.doc_text import read_doc_text
from doceditor.quote_anchor import _cp_at_u16

_OPEN = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})[ \t]*mermaid[ \t]*$", re.IGNORECASE)


def is_fence_quote(quote: str | None) -> bool:
    """Whether ``quote`` is the opening line of a mermaid fence, the anchor of a diagram pin."""
    return quote is not None and _OPEN.match(quote.strip("\n")) is not None


def block_at(text: str, from16: int | None) -> tuple[int, int, str] | None:
    """(first line, last line, source) of the mermaid block whose opening fence line holds the UTF-16 offset."""
    start = _cp_at_u16(text, from16)
    if start is None:
        return None
    lines = text.split("\n")
    position = 0
    for number, line in enumerate(lines):
        if position <= start <= position + len(line):
            opening = _OPEN.match(line)
            if opening is None:
                return None
            fence = opening.group("fence")
            closer = re.compile(rf"^[ \t]*{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
            for last in range(number + 1, len(lines)):
                if closer.match(lines[last]):
                    return number + 1, last + 1, "\n".join(lines[number + 1:last])
            return number + 1, len(lines), "\n".join(lines[number + 1:])
        position += len(line) + 1
    return None


def revision(text: str, from16: int | None) -> str | None:
    """The revision a pin is placed on: the hash of the block's source as it is now."""
    block = block_at(text, from16)
    return hashlib.sha256(block[2].strip().encode()).hexdigest() if block else None


def is_diagram_group(group: list[dict], path: str | None) -> bool:
    """Region comments with no image file, in a document that is not an image: pins on its diagrams."""
    return bool(group) and not group[0].get("image_src") and bool(path)


def mark_stale(group: Iterable[dict], path: str | None) -> None:
    """A document that cannot be read leaves its pins unmarked rather than calling them all stale."""
    try:
        text = read_doc_text(path)
    except (OSError, ValueError):
        return
    for row in group:
        current = revision(text, row.get("from_off"))
        row["region_stale"] = current is not None and row.get("image_rev") != current


def delivery_line(row: Mapping[str, Any], path: str | None) -> str:
    """The wake line that names the diagram a pin is on: where its block is, and its source."""
    name = (path or "the document").rsplit("/", 1)[-1]
    try:
        block = block_at(read_doc_text(path), row.get("from_off"))
    except (OSError, ValueError):
        block = None
    if block is None:
        return "Diagram: its source block could not be read."
    first, last, source = block
    return f"Diagram (mermaid block, lines {first} to {last} of {name}):\n```mermaid\n{source}\n```"
