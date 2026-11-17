"""Yjs-compatible forks and UTF-16 ranges mapped from UTF-8 Text deltas."""
from __future__ import annotations

import asyncio
from collections.abc import Collection, Sequence
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


def utf16_slice(text: str, start: int, end: int) -> str | None:
    """The text of a UTF-16 span, or None when it splits a character."""
    try:
        return text.encode("utf-16-le")[start * 2:end * 2].decode("utf-16-le")
    except UnicodeDecodeError:
        return None


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


def utf16_delta(before: str, delta: list[dict]) -> list[tuple[str, int]]:
    """One edit's UTF-8 Text delta as ``(kind, UTF-16 count)`` steps, kind being retain, delete or insert.

    It holds no text, so it can be kept and replayed without ``before``.
    Each retained/deleted byte segment is decoded once, never as a prefix.
    """
    raw = before.encode("utf-8")
    steps: list[tuple[str, int]] = []
    byte_position = 0
    for operation in delta:
        if "insert" in operation:
            steps.append(("insert", utf16_length(operation["insert"])))
            continue
        kind = "delete" if "delete" in operation else "retain"
        end_byte = byte_position + operation.get(kind, 0)
        steps.append((kind, utf16_length(raw[byte_position:end_byte].decode("utf-8"))))
        byte_position = end_byte
    return steps


def shift_offsets(edges: list[tuple[int, bool]], steps: Sequence[tuple[str, int]]) -> list[int]:
    """Map sorted UTF-16 edges through one edit's ``utf16_delta`` steps in one pass.

    A True association stays before insertion at that edge; False moves after.
    """
    ordered = sorted((offset, not right, index) for index, (offset, right) in enumerate(edges))
    mapped = [0] * len(edges)
    cursor = position = shift = 0
    for kind, count in steps:
        if kind == "insert":
            while cursor < len(ordered) and ordered[cursor][:2] <= (position, False):
                offset, _, index = ordered[cursor]
                mapped[index] = offset + shift
                cursor += 1
            shift += count
            continue
        end = position + count
        while cursor < len(ordered) and ordered[cursor][0] < end:
            offset, _, index = ordered[cursor]
            mapped[index] = (position if kind == "delete" else offset) + shift
            cursor += 1
        if kind == "delete":
            shift -= count
        position = end
    for offset, _, index in ordered[cursor:]:
        mapped[index] = offset + shift
    return mapped


def map_offsets(edges: list[tuple[int, bool]], before: str, delta: list[dict]) -> list[int]:
    """Map sorted UTF-16 edges through one UTF-8 Text delta over ``before``."""
    return shift_offsets(edges, utf16_delta(before, delta))


def _changes(steps: Sequence[tuple[str, int]]) -> list[tuple[int, int, int]]:
    """An edit's ``utf16_delta`` steps as ``(start, end, inserted)`` in the text before it, a delete and the insert beside it merged."""
    changes: list[list[int]] = []
    position = 0
    last: list[int] | None = None
    for kind, count in steps:
        if kind == "retain":
            position, last = position + count, None
            continue
        if last is None:
            last = [position, position, 0]
            changes.append(last)
        if kind == "delete":
            position += count
            last[1] = position
        else:
            last[2] += count
    return [(start, end, inserted) for start, end, inserted in changes]


def shift_spans(spans: Sequence[tuple[int, int]], steps: Sequence[tuple[str, int]]) -> list[tuple[int, int]]:
    """Map spans through one edit's ``utf16_delta`` steps with exclusive edges, following nothing typed over.

    A replaced span collapses: a selection still being commented on never
    moves onto text its author did not select.
    """
    edges = [(offset, right) for start, end in spans for offset, right in ((start, start == end), (end, True))]
    mapped = shift_offsets(edges, steps) if edges else []
    return [(first, max(first, last)) for first, last in zip(mapped[::2], mapped[1::2])]


def map_spans(spans: Sequence[tuple[int, int]], before: str, delta: list[dict]) -> list[tuple[int, int]]:
    """``shift_spans`` through one UTF-8 Text delta over ``before``."""
    return shift_spans(spans, utf16_delta(before, delta)) if spans else []


def follow_spans(spans: Sequence[tuple[int, int]], before: str, delta: list[dict],
                 open_ends: Collection[int] = ()) -> tuple[list[tuple[int, int]], set[int]]:
    """``follow_steps`` through one UTF-8 Text delta over ``before``."""
    return follow_steps(spans, utf16_delta(before, delta), open_ends) if spans else ([], set())


def follow_steps(spans: Sequence[tuple[int, int]], steps: Sequence[tuple[str, int]],
                 open_ends: Collection[int] = ()) -> tuple[list[tuple[int, int]], set[int]]:
    """Map comment spans through one edit's ``utf16_delta`` steps, and say which have an open end after it.

    The browser's ``cm/comment-range.ts`` follows the same rule. Edges are
    exclusive: text typed exactly at either edge lands outside. An edit that
    replaces exactly a span's text (the highlight selected and typed over)
    moves the span onto the new text and opens its end: while the edits after
    it only insert exactly at that end (the rest of the word being typed), they
    join it; the first edit anywhere else closes it. ``open_ends`` are the
    indices open before this edit. Removing the text with nothing in its place,
    or replacing more than it, collapses the span, the comment whose text is
    gone. A collapsed span stays before text typed at it.
    """
    moved = shift_spans(spans, steps)
    # Only a span this edit emptied can have been typed over.
    emptied = [index for index, ((start, end), (first, last)) in enumerate(zip(spans, moved)) if start < end and first == last]
    changes = _changes(steps) if open_ends or emptied else []
    opened: set[int] = set()
    for index in open_ends:
        start, end = spans[index]
        if all(first == last == end for first, last, _ in changes):
            moved[index] = (start, end + sum(inserted for *_, inserted in changes))
            opened.add(index)
    replaced = {(first, last) for first, last, inserted in changes if inserted and first < last}
    typed_over = [index for index in emptied if index not in opened and tuple(spans[index]) in replaced]
    if typed_over:
        wider = shift_offsets([edge for index in typed_over for edge in ((spans[index][0], True), (spans[index][1], False))], steps)
        for index, first, last in zip(typed_over, wider[::2], wider[1::2]):
            moved[index] = (first, last)
            opened.add(index)
    return moved, opened
