"""Workflow-attribution values that Plans stores without resolving skills."""

from __future__ import annotations

from typing import Any

import plans.service as service

DIFFICULTIES = frozenset({"routine", "standard", "hard"})
MAX_WORKFLOW_VALUE = 200
_DIFFICULTY_RUBRIC = (
    "workflow steps require difficulty. Ask: is the wanted result clear, is "
    "there a known way to do it here, and is it one piece? Three yes is "
    "routine, one no standard, more hard. Use routine, standard, or hard."
)


def difficulty_rubric() -> str:
    """Return the agent-facing missing-difficulty refusal text."""

    return _DIFFICULTY_RUBRIC


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise service.PlanInputError(f"{field} must be a non-empty string or null")
    normalized = value.strip()
    if len(normalized) > MAX_WORKFLOW_VALUE:
        raise service.PlanInputError(
            f"{field} exceeds the {MAX_WORKFLOW_VALUE}-character limit"
        )
    return normalized


def creation_values(
    *,
    workflow_ref: Any,
    workflow_version: Any,
    workflow_step: Any,
    difficulty: Any,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Normalize the opaque immutable fields before a node is inserted."""

    reference = _optional_text(workflow_ref, "workflow_ref")
    commit = _optional_text(workflow_version, "workflow_version")
    step = _optional_text(workflow_step, "workflow_step")
    if difficulty is not None and difficulty not in DIFFICULTIES:
        raise service.PlanInputError(
            "difficulty must be one of: routine, standard, hard"
        )
    if step is not None and difficulty is None:
        raise service.PlanInputError(difficulty_rubric())
    return reference, commit, step, difficulty


def rerating(value: Any) -> dict[str, str]:
    """Validate one immutable re-rating event payload."""

    if not isinstance(value, dict) or set(value) != {"level", "reason"}:
        raise service.PlanInputError(
            "difficulty_rerated must be {level, reason}"
        )
    level = value["level"]
    if level not in DIFFICULTIES:
        raise service.PlanInputError(
            "difficulty_rerated.level must be one of: routine, standard, hard"
        )
    reason = _optional_text(value["reason"], "difficulty_rerated.reason")
    if reason is None:
        raise service.PlanInputError("difficulty_rerated.reason is required")
    return {"level": level, "reason": reason}
