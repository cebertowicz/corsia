"""Shared parsing and insertion rules for ``Lessons`` list items."""

from __future__ import annotations

import re
from collections import Counter

_HEADING = re.compile(r"^#{1,6}\s+")
_LESSONS = re.compile(r"^##\s+Lessons\b")
_ITEM = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.+)$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def lesson_items(text: str) -> Counter[str]:
    """Return normalized list items under level-two Lessons headings outside fences."""
    inside = fenced = False
    current: list[str] = []
    items: Counter[str] = Counter()

    def finish() -> None:
        if current:
            items[" ".join(current)] += 1
            current.clear()

    for line in text.split("\n"):
        if _HEADING.match(line) and not fenced:
            finish()
            inside = bool(_LESSONS.match(line))
            continue
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if not inside or fenced:
            continue
        if (item := _ITEM.match(line)):
            finish()
            current.append(" ".join(item.group(1).split()))
        elif current and line.strip():
            current.append(" ".join(line.split()))
    finish()
    return items


def lesson_count(text: str) -> int:
    """Count lessons with the same wrapped-item semantics as the save gate."""
    return sum(lesson_items(text).values())


def append_lesson(text: str | None, line: str, day: str) -> str:
    """Insert one dated item in the first Lessons section, creating it if absent."""
    body = text if text is not None else "# Lessons\n"
    lesson = f"- {day}: {line}"
    if not re.search(r"^##\s+Lessons\b", body, re.MULTILINE):
        return body.rstrip("\n") + f"\n\n## Lessons\n{lesson}\n"
    lines = body.rstrip("\n").split("\n")
    start = next(i for i, row in enumerate(lines) if _LESSONS.match(row))
    end = next((i for i in range(start + 1, len(lines)) if _HEADING.match(lines[i])), len(lines))
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, lesson)
    return "\n".join(lines) + "\n"
