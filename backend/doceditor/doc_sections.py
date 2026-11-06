"""The sections of a Markdown document: each heading and the text under it, up to the next heading.

A section is named by its heading line as written, marks included ("## Build order"), so two
headings of one name at one level share a section. The text before the first heading is the
document's start, named "". A ``#`` line inside fenced code is text. Setext (underlined) headings
are not sections. Offsets are ``str`` indexes.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

# A line that may open a heading or a fence. Searched from its newline, which the regex engine
# finds fast; the document's first line is checked on its own.
_CANDIDATE = re.compile(r"\n {0,3}(?:#{1,6}(?=[ \t\n])|#{1,6}$|`{3,}|~{3,})", re.M)
_HEADING = re.compile(r" {0,3}(#{1,6})(?:[ \t]+(.*?))??(?:[ \t]+#+)?[ \t]*")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
#: How much of two texts ``changed_span`` compares at a time.
_CHUNK = 4096


def _line_end(text: str, start: int) -> int:
    end = text.find("\n", start)
    return len(text) if end < 0 else end


def _marks(text: str, end: int) -> Iterator[tuple[int, str]]:
    """Each heading line's start and its heading, up to the line holding ``end``."""
    fence: str | None = None
    starts = ([0] if text[:1] in (" ", "#", "`", "~") else []) + [
        match.start() + 1 for match in _CANDIDATE.finditer(text, 0, _line_end(text, end))]
    for start in starts:
        line = text[start:_line_end(text, start)]
        opened = _FENCE.match(line)
        if fence is not None:
            if opened is not None:
                run = opened.group(1)
                if run[:1] == fence[0] and len(run) >= len(fence) and not line[opened.end():].strip():
                    fence = None
            continue
        if opened is not None:
            fence = opened.group(1)
            continue
        heading = _HEADING.fullmatch(line)
        if heading is not None:
            title = (heading.group(2) or "").strip()
            yield start, f"{heading.group(1)} {title}".rstrip()


def headings(text: str) -> list[tuple[int, str]]:
    """Every section's start and heading, the document's start (0, "") first."""
    return [(0, ""), *_marks(text, len(text))]


def section_at(text: str, offset: int) -> str:
    """The heading of the section ``offset`` falls in; a heading line belongs to its own section."""
    found = ""
    for start, heading in _marks(text, offset):
        if start > offset:
            break
        found = heading
    return found


def sections_in(text: str, start: int, end: int) -> list[str]:
    """The headings of every section ``[start, end]`` touches, in document order, each once."""
    touched = [section_at(text, start)]
    touched += [heading for line, heading in _marks(text, end) if start < line <= end]
    return list(dict.fromkeys(touched))


def section_text(text: str, heading: str) -> str | None:
    """The first section named ``heading``, its heading line included; None when there is none."""
    marks = headings(text)
    for index, (start, found) in enumerate(marks):
        if found == heading:
            end = marks[index + 1][0] if index + 1 < len(marks) else len(text)
            return text[start:end]
    return None


def changed_span(before: str, after: str) -> tuple[int, int, int]:
    """Where two texts differ: (start, end in ``before``, end in ``after``).

    Compares in chunks first, then bisects the one chunk that differs, so no step walks characters.
    """
    limit = min(len(before), len(after))
    start = 0
    while start < limit and before[start:start + _CHUNK] == after[start:start + _CHUNK]:
        start += _CHUNK
    start = min(start + _common(before[start:start + _CHUNK], after[start:start + _CHUNK]), limit)
    tail, room = 0, limit - start
    while tail < room:
        size = min(_CHUNK, room - tail)
        if before[len(before) - tail - size:len(before) - tail] != after[len(after) - tail - size:len(after) - tail]:
            break
        tail += size
    size = min(_CHUNK, room - tail)
    left = before[len(before) - tail - size:len(before) - tail]
    right = after[len(after) - tail - size:len(after) - tail]
    tail += _common(left[::-1], right[::-1])
    return start, len(before) - tail, len(after) - tail


def _common(left: str, right: str) -> int:
    """How many leading characters two strings share."""
    low, high = 0, min(len(left), len(right))
    while low < high:
        middle = (low + high + 1) // 2
        if left[:middle] == right[:middle]:
            low = middle
        else:
            high = middle - 1
    return low


def name(heading: str) -> str:
    """How a wake names a section."""
    return f'the section "{heading}"' if heading else "the start of the document, before its first heading"
