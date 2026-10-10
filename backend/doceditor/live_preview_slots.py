"""Where a streamed edit lands in the live text, and what of its new text the browser still lacks.

A slot is one replacement a call will make: an ``edit_file``, one
``multi_edit`` entry, or a ``write_file``'s rewrite from its first changed
character. A slot is drawn only once its anchor is complete and matches the
live text exactly once; until then nothing is shown, whatever order the model
wrote the keys in. Ranges are UTF-16 offsets against the text the frame's
digest names, recomputed at every broadcast; the text is sent once, in order,
as appended pieces counted in UTF-16 units.
"""
from __future__ import annotations

from dataclasses import dataclass
from os.path import commonprefix

from doceditor.live_preview_args import ArgScanner, Value
from doceditor.live_text import utf16_length


@dataclass
class Slot:
    """What one slot has already sent: characters, the same count in UTF-16 units, and its last range."""

    sent: int = 0
    sent_u16: int = 0
    span: tuple[int, int] | None = None
    #: write_file only: how much of the content matched the document so far.
    prefix: int = 0
    #: doc_comment_create only: the kind last broadcast, comment or suggest.
    kind: str | None = None


def _unique(text: str, anchor: str) -> int | None:
    if not anchor:
        return None
    first = text.find(anchor)
    if first < 0 or text.find(anchor, first + 1) >= 0:
        return None
    return first


def _entries(tool: str, scanner: ArgScanner) -> list[dict[str, Value]]:
    return scanner.edits if tool == "multi_edit" else [scanner.fields]


def _edit_spans(entries: list[dict[str, Value]], scanner: ArgScanner, text: str) -> list[tuple[int, int, int, Value | None]]:
    """(slot, start, end, new text) for each edit whose anchor resolves to one place."""
    if scanner.flags.get("replace_all"):
        return []
    found: list[tuple[int, int, int, Value | None]] = []
    for index, entry in enumerate(entries):
        old = entry.get("old_string")
        if old is None or not old.complete:
            continue
        start = _unique(text, old.text())
        if start is None:
            continue
        end = start + len(old.text())
        if any(start < other_end and other_start < end for _i, other_start, other_end, _v in found):
            continue
        found.append((index, start, end, entry.get("new_string")))
    return found


def _write_span(scanner: ArgScanner, text: str, slot: Slot) -> tuple[int, int, int, str] | None:
    """The rewrite from the first character where the content leaves the document, once it has."""
    content = scanner.string("content")
    if content is None:
        return None
    written = content.text()
    if slot.span is None:
        checked = slot.prefix
        slot.prefix = checked + len(commonprefix([written[checked:], text[checked:]]))
        if slot.prefix == len(written) and not content.complete:
            return None  # everything so far matches the document; nothing to show yet
    start = min(slot.prefix, len(text))
    return 0, start, len(text), written[start:]


def _frame_slot(index: int, start: int | None, end: int, new: str, slot: Slot, text: str) -> dict | None:
    """One slot's frame part: its range, and only the new text appended since the last frame.

    A drawn slot whose anchor no longer matches (``start`` None) still sends its text with no
    range; the browser keeps the range it drew moving with its own edits.
    """
    span = None if start is None else (utf16_length(text[:start]), utf16_length(text[:end]))
    return frame_part(index, span, new, slot)


def frame_part(index: int, span: tuple[int, int] | None, new: str, slot: Slot) -> dict | None:
    """``_frame_slot`` for a range already in UTF-16 units, or None when nothing changed."""
    piece = new[slot.sent:]
    if not piece and (span is None or span == slot.span):
        return None
    part = {"slot": index, "from": span[0] if span else None, "to": span[1] if span else None,
            "at": slot.sent_u16, "text": piece}
    slot.sent, slot.sent_u16 = len(new), slot.sent_u16 + utf16_length(piece)
    slot.span = span or slot.span
    return part


def project_slots(tool: str, scanner: ArgScanner, text: str, slots: dict[int, Slot]) -> list[dict]:
    """The frame parts that changed since the last broadcast of this call."""
    if tool == "write_file":
        slot = slots.setdefault(0, Slot())
        rewrite = _write_span(scanner, text, slot)
        if rewrite is None:
            return []
        index, start, end, new = rewrite
        part = _frame_slot(index, start, end, new, slot, text)
        return [part] if part else []
    entries = _entries(tool, scanner)
    resolved: dict[int, tuple[int | None, int, Value | None]] = {
        index: (start, end, value) for index, start, end, value in _edit_spans(entries, scanner, text)}
    for index, slot in slots.items():
        if index not in resolved and slot.span is not None and index < len(entries):
            resolved[index] = (None, 0, entries[index].get("new_string"))
    parts = []
    for index, (begin, end, value) in sorted(resolved.items()):
        part = _frame_slot(index, begin, end, value.text() if value is not None else "", slots.setdefault(index, Slot()), text)
        if part:
            parts.append(part)
    return parts
