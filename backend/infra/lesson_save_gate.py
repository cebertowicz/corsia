"""The target-only save-time lesson rule and its post-save observation."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from loguru import logger

if TYPE_CHECKING:
    from infra.guidance_git import SaveResult


_HEADING = re.compile(r"^(?P<marks>#{1,6})\s+")
_LESSONS = re.compile(r"^##\s+Lessons\b")
_ITEM = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.+)$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


@dataclass(frozen=True)
class LessonSaveChange:
    """The only save-gate facts retained before a save can commit."""

    path: Path
    blocks: bool
    reworded: bool
    removed: bool
    items_before: int
    items_after: int


def lesson_items(text: str) -> Counter[str]:
    """Return normalized items under level-two Lessons headings outside fences.

    This richer parser is intentionally local to the save gate. Placement caps
    retain their legacy exact-heading, dash-item semantics in placement_decide.
    """
    inside = fenced = False
    current: list[str] = []
    items: Counter[str] = Counter()

    def finish() -> None:
        if current:
            items[" ".join(current)] += 1
            current.clear()

    for line in text.split("\n"):
        if (heading := _HEADING.match(line)) and not fenced:
            finish()
            # A Lessons section owns its lower-level subsections. Only a level-one
            # or level-two heading starts or ends that section.
            if len(heading.group("marks")) <= 2:
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


def _lesson_block(text: str) -> object | None:
    from core.lesson_trials import lesson_block
    from core.skill_validation import validate_skill_text

    return lesson_block(validate_skill_text(text).metadata or {})


def _lesson_change(path: Path, data: bytes) -> LessonSaveChange:
    """Compare only the target's old and proposed bytes for one pending write."""
    new = data.decode("utf-8", errors="replace")
    old = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    new_items, old_items = lesson_items(new), lesson_items(old)
    items_before, items_after = sum(old_items.values()), sum(new_items.values())
    removed = old_items - new_items
    blocks = items_after > items_before
    if path.name == "SKILL.md":
        blocks = blocks or (_lesson_block(new) is not None and _lesson_block(new) != _lesson_block(old))
    return LessonSaveChange(path, blocks, new_items != old_items, bool(removed), items_before, items_after)


def lesson_changes(writes: dict[Path, bytes]) -> tuple[LessonSaveChange, ...]:
    """Calculate target-only gate changes once for a pending save."""
    return tuple(_lesson_change(path, data) for path, data in writes.items())


def lesson_refusal(
    writes: dict[Path, bytes], author: str | None = None, *,
    changes: Iterable[LessonSaveChange] | None = None,
) -> SaveResult | None:
    """Return and record a refusal for a new lesson outside ``place``, if any."""
    from core.lesson_gate import SAVE_REASON, record_save_refusal, save_refusal_line
    from infra.guidance_git import SaveResult
    from infra.skill_save_validation import REFUSED

    assessed = tuple(changes) if changes is not None else lesson_changes(writes)
    refused = next(((change.path, change.removed) for change in assessed if change.blocks), None)
    if refused is None:
        return None
    path, removed = refused
    line = save_refusal_line(path, removed=removed)
    if warning := record_save_refusal(author):
        line = "\n".join((line, warning))
    return SaveResult(
        REFUSED, SAVE_REASON, line=line,
        errors=("a new lesson item or lesson block was added outside placement",),
    )


def observe_written_save(
    changes: Iterable[LessonSaveChange], author: str | None, *, stewardship_thread_id: str | None = None,
) -> str | None:
    """Record one post-commit observation, returning a visible warning on metric failure.

    A managed replacement observes its supplied target even when its lesson
    count did not change. Ordinary saves observe only a same-or-smaller lesson
    rewrite, against the saving agent's thread.
    """
    from core.lesson_gate import record_save_observation
    from infra.guidance_messages import saver_thread

    assessed = tuple(changes)
    observed = tuple(change for change in assessed if change.reworded)
    if stewardship_thread_id is not None:
        thread_id, reason, counted = stewardship_thread_id, "stewardship", assessed
    elif observed:
        thread_id, reason, counted = saver_thread(author), "rewording", observed
    else:
        return None
    try:
        record_save_observation(
            thread_id, reason=reason,
            items_before=sum(change.items_before for change in counted),
            items_after=sum(change.items_after for change in counted),
        )
    except Exception:
        logger.opt(exception=True).warning("lesson save observation could not be recorded")
        return "Saved, but the lesson observation could not be recorded. Report this."
    return None
