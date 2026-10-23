"""The save-time lesson rule: an agent's lesson reaches a guidance file only through ``place``.

A write is judged against the file on disk. A new or changed line under
``## Lessons`` is refused; the section compares as a multiset of lines, so
deletions and reorders pass. A SKILL.md is refused only when it is new with a
``lesson:`` block or its block changes; edits to its body pass.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.guidance_git import SaveResult

_HEADING = re.compile(r"^#{1,2} ")
_LESSONS = re.compile(r"^## Lessons\s*$")


def _lesson_lines(text: str) -> Counter[str]:
    inside = False
    lines: Counter[str] = Counter()
    for line in text.split("\n"):
        if _HEADING.match(line):
            inside = bool(_LESSONS.match(line))
        elif inside and line.strip():
            lines[line.strip()] += 1
    return lines


def _lesson_block(text: str) -> object | None:
    from core.lesson_trials import lesson_block
    from core.skill_validation import validate_skill_text

    return lesson_block(validate_skill_text(text).metadata or {})


def _adds_lesson(path: Path, data: bytes) -> bool:
    new = data.decode("utf-8", errors="replace")
    old = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    if _lesson_lines(new) - _lesson_lines(old):
        return True
    if path.name != "SKILL.md":
        return False
    block = _lesson_block(new)
    return block is not None and block != _lesson_block(old)


def lesson_refusal(writes: dict[Path, bytes], author: str | None = None) -> SaveResult | None:
    """The guidance-save result refusing a lesson written outside placement, or None.

    A refusal is recorded as an outcome fact for the author's thread.
    """
    from core.lesson_gate import SAVE_REASON, record_save_refusal, save_refusal_line
    from infra.guidance_git import SaveResult
    from infra.skill_save_validation import REFUSED

    path = next((path for path, data in writes.items() if _adds_lesson(path, data)), None)
    if path is None:
        return None
    record_save_refusal(author)
    return SaveResult(
        REFUSED, SAVE_REASON, line=save_refusal_line(path),
        errors=("a lesson line or lesson block was added or changed outside placement",),
    )
