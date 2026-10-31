"""Workflow-attribution values that Plans stores without resolving skills."""

from __future__ import annotations

import json
from typing import Any, Literal

import plans.service as service
from core.size_rating import UNKNOWN as UNKNOWN_SIZE

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


#: The add reply's line once a phase's run is built.
STEPS_CREATED = (
    "Created this run's steps. Skip an optional one with close_node and a reason."
)
#: Why a required step can't be closed or archived as not done.
REQUIRED_STEP_SKIP = (
    "required workflow step {step} can't be skipped: close it with evidence "
    "(mark_done, request_review, accept) or fail it"
)


def build_run(db, phase, steps, *, causing_executor: str | None) -> list[dict[str, Any]]:
    """Create one child per resolved step of the run ``phase`` starts, in definition order.

    ``steps`` are the plain steps ``tools.workflow_task_fields`` resolved off the
    database. Every step carries the run's intent and records no actor (a trusted
    writer), so its instruction and check reach the agent the step is handed to.
    A step naming a workflow becomes a child phase built the same way.
    """
    from plans import dependencies, intent_words, node_creation

    intent_node_id = intent_words.canonical(db, phase.id)["id"]
    built: dict[str, Any] = {}
    listed = []
    for step in steps:
        title = step.get("title") or step["id"]
        difficulty = step.get("difficulty") or phase.difficulty
        if difficulty is None:
            raise service.PlanInputError(f"step {step['id']}: {difficulty_rubric()}")
        nested = step.get("workflow")
        by = step.get("by", "agent")
        check = step.get("check") or f"Done: {title}"
        description = step.get("instruction")
        if by == "person":
            from plans.boundary_fields import PERSON_STEP_DESCRIPTION_LIMIT
            description = description or title
            if step.get("check"):
                description += f"\n\nCheck: {check}"
            if len(description) > PERSON_STEP_DESCRIPTION_LIMIT:
                raise service.PlanInputError(
                    f"step {step['id']}: a person step's instruction must fit "
                    f"{PERSON_STEP_DESCRIPTION_LIMIT} characters; shorten its SKILL.md section"
                )
        node = node_creation.create_node(
            db, phase.plan_id, kind="task" if nested is None else "phase", title=title,
            acceptance_criteria=check, parent_id=phase.id,
            order_key=None, description=description,
            workflow_ref=nested or phase.workflow_ref,
            workflow_version=step.get("version") if nested else phase.workflow_version,
            workflow_step=step["id"], difficulty=difficulty, size=UNKNOWN_SIZE,
            checkpoint_exception=False, assignee="user" if by == "person" else None, spawn_kind=None,
            watchdog_budget_seconds=None, follow_up_of=None, actor_id=None,
            provenance="plans.workflow_run", node_id=service._id(),
            intent_node_id=intent_node_id, required_step=step.get("required") is True, workflow_by=by,
            person_step=not (by == "person" and step.get("after")),
        )
        built[step["id"]] = node
        entry = {"id": node.id, "title": title, "required": step.get("required") is True}
        if by != "agent":
            entry["by"] = by
        if nested:
            entry["steps"] = build_run(db, node, step.get("steps") or (), causing_executor=causing_executor)
        listed.append(entry)
    for step in steps:
        for prior in step.get("after") or ():
            dependencies.add_dependency(
                built[step["id"]].id, built[prior].id, provenance="plans.workflow_run",
                causing_executor=causing_executor, db=db,
            )
    return listed


def added(db, node, line, steps, *, causing_executor: str | None):
    """The add reply: the node, its rank line, and the run built under a workflow phase."""
    from plans.boundary import node_projection

    listed = build_run(db, node, steps, causing_executor=causing_executor) if steps else []
    if line is None and not listed:
        return node
    result: dict[str, Any] = {"node": node_projection(service._node_from_row(db, service._node_row(db, node.id)))}
    if line is not None:
        result["rank"] = line
    if listed:
        result["steps"] = listed
        result["notice"] = STEPS_CREATED
    return result


REVIEWER_ROUTE = (
    "This step is checked by someone else: hand it to a reviewer "
    "(delegate_task with plan_node_id) and accept or send back what they return."
)


def _open_step(db, node_id: str):
    """An unfinished workflow step's node row and creation payload, the one record of its flags."""
    node = service._node_row(db, node_id)
    if node["workflow_step"] is None or node["state"] in service._TERMINAL_STATES:
        return None, {}
    return node, creation_payload(db, node_id)


def creation_payload(db, node_id: str) -> dict[str, Any]:
    """A node's creation-event payload, where a workflow step's flags are recorded."""
    row = db.execute(
        "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='node_created' "
        "ORDER BY created_at,id LIMIT 1", (node_id,),
    ).fetchone()
    return json.loads(row["payload_json"] or "{}") if row is not None else {}


NOT_A_STEP = {"workflow_ref": None, "workflow_version": None, "workflow_step": None, "difficulty": None}


def same_step_fields(db, source) -> dict[str, Any]:
    """The creation fields of a send-back's revision: the same step, tried again, under the same rules."""
    if source["workflow_step"] is None:
        return NOT_A_STEP
    created = creation_payload(db, source["id"])
    return {
        **{field: source[field] for field in NOT_A_STEP},
        "required_step": created.get("workflow_required") is True,
        "workflow_by": created.get("workflow_by", "agent"),
    }


#: What a follow-up keeps of its source's step: nothing, the step it counts as, or that step under its rules.
FollowUpStep = Literal["none", "identity", "rules"]


def follow_up_step_fields(db, source, step: FollowUpStep) -> dict[str, Any]:
    """A follow-up's creation fields: an accepted step's follow-up counts as that step, a revision is it."""
    if step == "rules":
        return same_step_fields(db, source)
    if step == "identity":
        return {field: source[field] for field in NOT_A_STEP}
    return NOT_A_STEP


def refuse_reviewer_self_close(db, node_id: str, operation: str) -> None:
    """Require a harvested worker return before a reviewer step is marked done or accepted."""
    from plans import delegation_harvest

    if operation not in {"mark_done", "accept", "accept_with_follow_up"}:
        return
    node, created = _open_step(db, node_id)
    if created.get("workflow_by") != "reviewer":
        return
    if operation != "mark_done" and delegation_harvest.harvest_review(db, node) is not None:
        return
    raise service.PlanError(REVIEWER_ROUTE)


def refuse_required_skip(db, node_id: str) -> None:
    """Refuse closing or archiving an unfinished required step of a workflow run."""
    node, created = _open_step(db, node_id)
    if created.get("workflow_required") is True:
        raise service.PlanError(REQUIRED_STEP_SKIP.format(step=node["workflow_step"]))
