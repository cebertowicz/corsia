"""Yjs-compatible forks and UTF-16 ranges mapped from UTF-8 Text deltas."""
from __future__ import annotations

import asyncio
import hashlib

from doceditor.live_diff import text_changes, text_changes_async

from pycrdt import Doc, Text


def clone(update: bytes) -> Doc:
    doc: Doc = Doc()
    doc["text"] = Text()
    doc.apply_update(update)
    return doc


def create(text: str) -> Doc:
    doc: Doc = Doc()
    doc["text"] = Text(text)
    return doc


def _apply_changes(doc: Doc, changes: list[tuple[int, int, str]], shift: int = 0) -> int:
    text = doc["text"]
    with doc.transaction():
        for start, end, inserted in changes:
            if end > start:
                del text[start + shift:end + shift]
            if inserted:
                text.insert(start + shift, inserted)
            shift += len(inserted.encode("utf-8")) - (end - start)
    return shift


def apply_text(doc: Doc, new: str) -> None:
    """Apply a bounded diff to the fork's items; concurrent items stay untouched."""
    _apply_changes(doc, text_changes(str(doc["text"]), new))


async def apply_text_async(doc: Doc, new: str) -> None:
    """Prepare immutable opcodes off the loop; apply CRDT changes on this owner."""
    old = str(doc["text"])
    if old == new:
        return
    changes = await text_changes_async(old, new)
    shift = 0
    # These are private candidate forks, never a published live document. Keep
    # mutation on this owner, but let unrelated socket/Stop work run between
    # bounded groups of CRDT operations rather than one large transaction.
    for first in range(0, len(changes), 128):
        shift = _apply_changes(doc, changes[first:first + 128], shift)
        await asyncio.sleep(0)


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def byte_offset(text: str, offset: int) -> int:
    """Convert a character-aligned UTF-16 position at the pycrdt boundary."""
    data = text.encode("utf-16-le")
    if not 0 <= offset <= len(data) // 2:
        raise ValueError("Offset is outside the document")
    try:
        prefix = data[:offset * 2].decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise ValueError("Offset splits a Unicode character") from exc
    return len(prefix.encode("utf-8"))


def text_digest(text: str) -> str:
    """Wire digest: first 16 lowercase hex characters of SHA-256 over UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def state_clocks(vector: bytes) -> dict[int, int]:
    """Decode a Yjs state vector to its client-clock map.

    Libraries order a vector's entries differently, so two encodings of one
    state are equal only as maps.
    """
    position = 0
    def varuint() -> int:
        nonlocal position
        value = shift = 0
        while True:
            if position >= len(vector):
                raise ValueError("State vector is truncated")
            byte = vector[position]
            position += 1
            value |= (byte & 0x7F) << shift
            if byte < 0x80:
                return value
            shift += 7
    clocks: dict[int, int] = {}
    for _ in range(varuint()):
        client = varuint()
        clocks[client] = varuint()
    if position != len(vector):
        raise ValueError("State vector has trailing bytes")
    return clocks


def map_offsets(edges: list[tuple[int, bool]], before: str, delta: list[dict]) -> list[int]:
    """Map sorted UTF-16 edges and delta segments in one pass over one buffer.

    A True association stays before insertion at that edge; False moves after.
    Each retained/deleted byte segment is decoded once, never as a prefix.
    """
    raw = before.encode("utf-8")
    ordered = sorted((offset, not right, index) for index, (offset, right) in enumerate(edges))
    mapped = [0] * len(edges)
    cursor = position = byte_position = shift = 0
    for operation in delta:
        if "insert" in operation:
            while cursor < len(ordered) and ordered[cursor][:2] <= (position, False):
                offset, _, index = ordered[cursor]
                mapped[index] = offset + shift
                cursor += 1
            shift += utf16_length(operation["insert"])
            continue
        count = operation.get("retain", operation.get("delete", 0))
        end_byte = byte_position + count
        end = position + utf16_length(raw[byte_position:end_byte].decode("utf-8"))
        while cursor < len(ordered) and ordered[cursor][0] < end:
            offset, _, index = ordered[cursor]
            mapped[index] = (position if "delete" in operation else offset) + shift
            cursor += 1
        if "delete" in operation:
            shift -= end - position
        position, byte_position = end, end_byte
    for offset, _, index in ordered[cursor:]:
        mapped[index] = offset + shift
    return mapped


def map_offset(offset: int, before: str, delta: list[dict], *, right: bool) -> int:
    """Map one edge with the same exclusive-boundary semantics as all ranges."""
    return map_offsets([(offset, right)], before, delta)[0]
