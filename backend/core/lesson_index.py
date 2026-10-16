"""Which trial lessons a newly installed skills index offers.

Trial lessons are ordinary resolved skills until this one decision: a trial is
offered only while its file is in its owner's own skills and says ``trial``,
its review date is ahead, and its owner holds an open review of that exact
version. Everything else is kept out with a reason the skills surface shows.
The skills registry calls this once per install, so the choice is frozen with
the installed index: a new or expired trial changes the next install, never a
running conversation.

The same rendered index line is the unit of the lesson budget and of every
later per-lesson estimate, so the parts never add up to more than the index.
That line marks a trial, so a reader of the index can tell an unproven rule
its owner wrote from an approved skill before loading it.

A trial never stands in for an ordinary skill. Placement refuses a taken name,
but an ordinary skill can take a trial's name afterwards; scope precedence
would then let the more specific trial win. From the next install the trial
is held and the ordinary skill offered. Both files stay as they are. A System
skill of the name already wins in the resolver.

One walk decides each name, from its most specific row down. Where an ordinary
skill holds the name, every trial above it is held and it is offered. Otherwise
the first trial that passes the checks stands for the name, with its lesson
record and its place in the budget: a held trial never gives way to a trial
that was not checked the same way.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from core.lesson_files import raw_sha256, read_trial
from core.lesson_trials import INDEX_TOKEN_BUDGET, lesson_block, offer_refusal
from core.skill_registry_types import SkillRow
from core.skill_scanning import omitted_row

OVERRIDDEN = "overridden by a more specific skill"
OVER_BUDGET = "the lesson index budget is full"
UNCHECKED = "its review could not be checked"
UNREADABLE = "its file could not be read"
SAME_NAME = "an ordinary skill has its name"
MISPLACED = "its file is not in its owner's skills"
HELD_PREFIX = "trial lesson not offered: "
#: Follows a trial's name on its index line; an ordinary skill has none.
TRIAL_MARK = " (trial, unproven)"


@dataclass(frozen=True)
class LessonPartition:
    """One resolved skill set split at an install."""

    offered: list[SkillRow]
    omitted: list[SkillRow]
    lessons: list[dict[str, Any]]


def index_line(row: SkillRow) -> str:
    """The one line a skill contributes to the delivered index."""
    trial = lesson_block(row.metadata) is not None
    return line_for(row.name, row.description, row.path, trial=trial)


def line_for(name: str, description: str, path: object, *, trial: bool = False) -> str:
    """The index line a skill with these fields would contribute."""
    return f"- {name}{TRIAL_MARK if trial else ''}: {description} ({path})"


def fits_budget(lessons: list[dict[str, Any]], line: str, lesson_id: str) -> bool:
    """Whether one more trial's line fits beside the offered ones.

    ``lessons`` is an install's offered list; the candidate's own earlier
    version does not count against it.
    """
    spent = sum(
        line_tokens(str(lesson["index_line"])) for lesson in lessons if lesson["id"] != lesson_id
    )
    return spent + line_tokens(line) <= INDEX_TOKEN_BUDGET


def line_tokens(line: str, model: str | None = None) -> int:
    """The Skills estimator applied to one index line."""
    from core.token_estimation import _text_tokens

    return _text_tokens(line, model)


def descriptor_estimates(
    lessons: list[dict[str, Any]], *, skills_tokens: int, model: str | None = None,
) -> dict[str, int]:
    """Per-lesson index cost by id, never summing to more than the Skills total."""
    raw = {str(lesson["id"]): line_tokens(str(lesson["index_line"]), model) for lesson in lessons}
    total = sum(raw.values())
    if total <= skills_tokens or total == 0:
        return raw
    return {key: tokens * max(0, skills_tokens) // total for key, tokens in raw.items()}


def _review_state(db, lesson, behavioral_sha256: str) -> tuple[str | None, str]:
    """Why the trial's review does not cover it, and when that review opened."""
    from plans import public as plans

    if db is None:
        return UNCHECKED, ""
    node_id = lesson.review_node_id
    try:
        refusal = plans.lesson_review_refusal(
            db, node_id, owner_thread_id=lesson.owner, lesson_id=lesson.id, state=lesson.state,
            behavioral_sha256=behavioral_sha256,
        )
        return refusal, plans.lesson_review_opened_at(db, node_id) or ""
    except Exception as failure:  # noqa: BLE001 - an uncheckable review never offers a trial
        from loguru import logger

        logger.warning(
            "lesson.review_unchecked lesson={} node={} error={}", lesson.id, node_id, failure,
        )
        return UNCHECKED, ""


def _candidate(row: SkillRow, *, thread_id: str, db, now: datetime):
    """One trial row as (refusal, opened_at, record)."""
    try:
        data = row.path.read_bytes()
        trial = read_trial(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError):
        trial = None
    if trial is None:
        return UNREADABLE, "", None
    # The review covers the owner's own file: a copy anywhere else is not the trial.
    if row.scope != "thread" or row.scope_id != trial.lesson.owner:
        return MISPLACED, "", None
    refusal = offer_refusal(trial.lesson, now)
    opened = ""
    if refusal is None:
        refusal, opened = _review_state(db, trial.lesson, trial.behavioral_sha256)
    return refusal, opened, {
        "id": trial.lesson.id,
        "behavioral_sha256": trial.behavioral_sha256,
        "name": row.name,
        "scope": row.scope,
        "scope_id": row.scope_id,
        "inherited": not (row.scope == "thread" and row.scope_id == thread_id),
        "path": str(row.path),
        "raw_sha256": raw_sha256(data),
        "review_after": trial.lesson.review_after,
        "index_line": index_line(row),
    }


@dataclass(frozen=True)
class _Standing:
    """What one name comes to: the row that stands for it and the trials passed over."""

    row: SkillRow | None
    opened: str
    record: dict[str, Any] | None
    held: list[SkillRow]


def _taken_back(row: SkillRow) -> SkillRow:
    """An overridden row as the one now standing for its name."""
    return replace(row, status="included", errors=[], overrides=None)


def _standing(winner: SkillRow, remaining: list[SkillRow], *, thread_id: str, db, now) -> _Standing:
    """Walk a trial's name down the rows it overrode to the one the index may offer.

    Rows the walk settles leave ``remaining``; a row under the one that stands
    stays there, overridden.
    """
    below = [
        row for row in reversed(remaining)
        if row.name == winner.name and row.overrides == winner.name and row.errors == [OVERRIDDEN]
    ]
    ordinary = next((row for row in below if lesson_block(row.metadata) is None), None)
    held: list[SkillRow] = []
    for row in (winner, *below):
        if row is not winner:
            remaining[:] = [other for other in remaining if other is not row]
        if row is ordinary:
            return _Standing(_taken_back(row), "", None, held)
        refusal, opened, record = _candidate(row, thread_id=thread_id, db=db, now=now)
        if refusal is None and ordinary is not None:
            refusal = SAME_NAME
        if refusal is None:
            return _Standing(row if row is winner else _taken_back(row), opened, record, held)
        held.append(omitted_row(row, HELD_PREFIX + refusal))
    return _Standing(None, "", None, held)


def partition(
    included: list[SkillRow], omitted: list[SkillRow], *, thread_id: str, db,
    now: datetime | None = None,
) -> LessonPartition:
    """Split resolved skills into the offered index, the held rows and the lesson list.

    Trials enter the budget in the order their reviews opened, so a newer
    trial never displaces one already offered.
    """
    moment = now or datetime.now(timezone.utc)
    remaining = list(omitted)
    held: list[SkillRow] = []
    stands: list[SkillRow | None] = []
    admitted: list[tuple[str, str, int, dict[str, Any]]] = []
    for index, row in enumerate(included):
        if lesson_block(row.metadata) is None:
            stands.append(row)
            continue
        standing = _standing(row, remaining, thread_id=thread_id, db=db, now=moment)
        held.extend(standing.held)
        stands.append(standing.row)
        if standing.record is not None:
            admitted.append((standing.opened, standing.record["id"], index, standing.record))
    lessons: list[dict[str, Any]] = []
    spent = 0
    for _opened, _id, index, record in sorted(admitted):
        cost = line_tokens(record["index_line"])
        if spent + cost > INDEX_TOKEN_BUDGET:
            # No ordinary skill is under a trial that passed, so nothing takes its place.
            held.append(omitted_row(stands[index], HELD_PREFIX + OVER_BUDGET))
            stands[index] = None
            continue
        spent += cost
        lessons.append(record)
    offered = [row for row in stands if row is not None]
    return LessonPartition(offered=offered, omitted=[*remaining, *held], lessons=lessons)
