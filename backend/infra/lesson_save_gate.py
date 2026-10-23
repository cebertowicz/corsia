"""The save-time lesson rule: an agent's new lesson reaches a guidance file through ``place``.

A write is judged against the file on disk. It refuses a growing multiset of
``## Lessons`` items only when an added item is not already in another guidance
file of the same scope. Equal-or-smaller rewrites pass and are counted. A
SKILL.md is refused only when it is new with a ``lesson:`` block or its block
changes; edits to its body pass.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.guidance_git import SaveResult

from core.lesson_items import lesson_items


def _lesson_block(text: str) -> object | None:
    from core.lesson_trials import lesson_block
    from core.skill_validation import validate_skill_text

    return lesson_block(validate_skill_text(text).metadata or {})


def _same_scope_items(path: Path, workspace: Path | None, scope: str | None) -> Counter[str]:
    """Current lesson items in other on-disk guidance files of this save's scope."""
    if workspace is None or not scope:
        return Counter()
    from core.guidance_paths import classify_guidance_path
    from infra.database import get_db

    items: Counter[str] = Counter()
    db = get_db()
    try:
        for candidate in workspace.rglob("*"):
            if candidate == path or not candidate.is_file():
                continue
            if classify_guidance_path(candidate, workspace=workspace, db=db) == scope:
                items.update(lesson_items(candidate.read_text(encoding="utf-8", errors="replace")))
    finally:
        db.close()
    return items


def _lesson_change(
    path: Path, data: bytes, *, workspace: Path | None, scope: str | None,
) -> tuple[bool, bool, bool, int, int]:
    """Return the gate result and canonical lesson-item counts for one file write."""
    new = data.decode("utf-8", errors="replace")
    old = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    new_items, old_items = lesson_items(new), lesson_items(old)
    items_before, items_after = sum(old_items.values()), sum(new_items.values())
    added, removed = new_items - old_items, old_items - new_items
    if items_after > items_before and any(item not in _same_scope_items(path, workspace, scope) for item in added):
        return True, False, bool(removed), items_before, items_after
    if path.name != "SKILL.md":
        return False, new_items != old_items, bool(removed), items_before, items_after
    block = _lesson_block(new)
    return block is not None and block != _lesson_block(old), new_items != old_items, bool(removed), items_before, items_after


def lesson_refusal(
    writes: dict[Path, bytes], author: str | None = None, *, workspace: Path | None = None,
    scope: str | None = None,
) -> SaveResult | None:
    """The guidance-save result refusing a lesson written outside placement, or None.

    A refusal is recorded as an outcome fact for the author's thread.
    """
    from core.lesson_gate import SAVE_REASON, record_save_refusal, save_refusal_line
    from infra.guidance_git import SaveResult
    from infra.skill_save_validation import REFUSED

    changes = [
        (path, *_lesson_change(path, data, workspace=workspace, scope=scope))
        for path, data in writes.items()
    ]
    refused = next(((path, removed) for path, blocks, _reworded, removed, _before, _after in changes if blocks), None)
    if refused is None:
        reworded_changes = [
            (items_before, items_after)
            for _path, _blocks, reworded, _removed, items_before, items_after in changes
            if reworded
        ]
        if reworded_changes:
            from core.lesson_gate import record_save_reword

            for items_before, items_after in reworded_changes:
                record_save_reword(author, items_before=items_before, items_after=items_after)
        return None
    path, removed = refused
    record_save_refusal(author)
    return SaveResult(
        REFUSED, SAVE_REASON, line=save_refusal_line(path, removed=removed),
        errors=("a new lesson item or lesson block was added outside placement",),
    )
