"""Workflow-step validation for a skill's CORSIA data, wherever it is filed."""

from __future__ import annotations

from typing import Any

from core.workflow_result import schema_errors

_WORKFLOW_STEP_KEYS = frozenset(
    {
        "id",
        "title",
        "required",
        "check",
        "difficulty",
        "quality",
        "after",
        "replaces",
        "workflow",
    }
)
_WORKFLOW_DIFFICULTIES = frozenset({"routine", "standard", "hard"})


def text_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _workflow_step_shape_errors(step: dict[str, Any], label: str) -> list[str]:
    """Validate workflow fields whose values have a documented v1 shape."""
    errors: list[str] = []
    for key in step:
        if key == "models":
            errors.append(f"{label}.models is reserved and not allowed in v1")
        elif key not in _WORKFLOW_STEP_KEYS:
            errors.append(f"{label}.{key} is an unknown key")
    difficulty = step.get("difficulty")
    if difficulty is not None:
        if not isinstance(difficulty, dict):
            errors.append(f"{label}.difficulty must be a mapping")
        elif "default" in difficulty and difficulty["default"] not in _WORKFLOW_DIFFICULTIES:
            errors.append(
                f"{label}.difficulty.default must be one of routine, standard, hard"
            )
    quality = step.get("quality")
    if quality is not None and (
        not isinstance(quality, list) or any(not isinstance(item, dict) for item in quality)
    ):
        errors.append(f"{label}.quality must be a list of mappings")
    return errors


def validate_workflow(corsia: dict[str, Any], *, skill_name: str | None = None) -> list[str]:
    """Validate checkable workflow steps without prescribing their full shape."""

    workflow = corsia.get("workflow")
    if workflow is None:
        return []
    if not isinstance(workflow, dict):
        return ["corsia.workflow must be a mapping"]
    # A worker's workflow may declare a result and no steps, so the result is checked first.
    errors: list[str] = (
        schema_errors(workflow["result"]) if "result" in workflow else []
    )
    steps = workflow.get("steps")
    if steps is None:
        return errors
    if not isinstance(steps, list):
        return [*errors, "corsia.workflow.steps must be a list"]

    known: set[str] = set()
    after_by_id: dict[str, list[str]] = {}
    for index, step in enumerate(steps):
        prefix = f"corsia.workflow.steps[{index}]"
        if not isinstance(step, dict):
            errors.append(f"{prefix} must be a mapping")
            continue
        step_id = text_value(step.get("id"))
        step_label = f"{prefix} {step_id!r}" if step_id else prefix
        errors.extend(_workflow_step_shape_errors(step, step_label))
        if "workflow" in step:
            reference = text_value(step["workflow"])
            if reference is None:
                errors.append(f"{step_label}.workflow must be a non-empty string")
            elif reference == skill_name:
                errors.append(f"{step_label}.workflow cannot name its own skill")
        if not step_id:
            errors.append(f"{prefix}.id is required")
            continue
        if step_id in known:
            errors.append(f"{prefix}.id duplicates {step_id!r}")
            continue
        known.add(step_id)
        if step.get("required") is True and not text_value(step.get("check")):
            errors.append(f"{prefix}.check is required when required is true")
        after = step.get("after", [])
        if not isinstance(after, list) or any(not text_value(value) for value in after):
            errors.append(f"{prefix}.after must be a list of step ids")
            continue
        after_by_id[step_id] = [str(value).strip() for value in after]

    for step_id, after in after_by_id.items():
        for dependency in after:
            if dependency not in known:
                errors.append(
                    f"corsia.workflow.steps {step_id!r}.after references unknown "
                    f"step {dependency!r}"
                )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str, trail: list[str]) -> None:
        if step_id in visited:
            return
        if step_id in visiting:
            cycle = trail[trail.index(step_id):] + [step_id]
            errors.append("corsia.workflow.steps has a cycle: " + " -> ".join(cycle))
            return
        visiting.add(step_id)
        for dependency in after_by_id.get(step_id, []):
            if dependency in known:
                visit(dependency, [*trail, step_id])
        visiting.remove(step_id)
        visited.add(step_id)

    for step_id in after_by_id:
        visit(step_id, [])
    return errors
