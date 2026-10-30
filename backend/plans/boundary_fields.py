"""Accepted parameter vocabulary for actor-bound plan mutations."""

from __future__ import annotations

from typing import Any

import plans.service as service
from plans.size_fields import require_task_size, validate_size

#: Re-exported so this module's raise sites read against one taxonomy. The class
#: is owned by `plans.errors`.
PlanInputError = service.PlanInputError

USER_STEP_TRIED_MARKER = "Already tried:"
USER_STEP_DESCRIPTION_REFUSAL = (
    "user steps need instructions with 'Already tried: …'; add what you tried"
)
PERSON_STEP_DESCRIPTION_LIMIT = 400
PERSON_STEP_DESCRIPTION_LIMIT_REFUSAL = (
    "Put the how-to in a document, register it and pass instructions_artifact_id; "
    "keep the description to one line."
)

_CREATE = {
    "kind",
    "title",
    "acceptance_criteria",
    "parent_id",
    "description",
    "workflow_ref",
    "workflow_step",
    "difficulty",
    "size",
    "intent",
    "intent_words",
    "checkpoint_exception",
    "assignee",
    "spawn_kind",
    "watchdog_budget_seconds",
    "depends_on_node_id",
    "instructions_artifact_id",
}

#: Operations that end their executor's work on a task.
CLOSING_OPERATIONS = frozenset({
    "request_review", "mark_done", "fail", "accept", "accept_with_follow_up",
    "close_node", "archive_node",
})
#: Operations that take the executor off a task without closing it.
PARK_OPERATIONS = frozenset({"pause_node", "block_node", "arm_watch"})
#: Every close and every park. Only these accept an optional ``next_node_id``
#: to start next, and only their results carry ``next``: what now holds that
#: executor's slot and its other open tasks.
NEXT_OPERATIONS = CLOSING_OPERATIONS | PARK_OPERATIONS
#: Operations only the authenticated user applies, over HTTP. The agent tool
#: schema never lists them: only the user pauses or resumes (pause contract
#: rule 1). An agent schedules a task instead (``plans.scheduled_wait``).
HUMAN_OPERATIONS = frozenset({"resume", "pause", "pause_node"})
_NEXT = {"next_node_id"}

OPERATION_FIELDS = {
    "add": _CREATE,
    "pause": {"expected_revision"},
    "resume": {"expected_revision"},
    "insert": _CREATE | {"before_node_id"},
    "edit": {
        "node_id",
        "title",
        "description",
        "acceptance_criteria",
        "intent",
        "intent_words",
        "watchdog_budget_seconds",
        "difficulty_rerated",
        "size",
        "expected_revision",
    },
    "split": {
        "node_id",
        "title",
        "acceptance_criteria",
        "description",
        "size",
        "intent",
        "intent_words",
        "expected_revision",
    },
    "reorder": {"node_id", "order_key", "expected_revision"},
    # Positioning in the dependency graph, not settlement — same shape as
    # reorder, and deliberately absent from node_settlement.SETTLING_OPERATIONS.
    "add_dependency": {"node_id", "depends_on_node_id", "expected_revision"},
    "remove_dependency": {"node_id", "depends_on_node_id", "expected_revision"},
    # A non-blocking sidetrack from a live node, and the retirement of one.
    "add_follow_up": {
        "node_id",
        "title",
        "acceptance_criteria",
        "size",
        "intent",
        "intent_words",
        "expected_revision",
    },
    "archive_node": {"node_id", "node_ids", "expected_revision"} | _NEXT,
    "close_node": {"node_id", "reason", "expected_revision"} | _NEXT,
    # The user supplies no evidence: user_settle records their one-tap verdict.
    # The agent schema makes evidence required (tools.plan_schemas).
    "mark_done": {"node_id", "evidence", "verification_result", "expected_revision"} | _NEXT,
    "activate": {"node_id", "expected_revision"},
    "start": {"node_id", "expected_revision"},
    "pause_node": {"node_id", "reason", "expected_revision"} | _NEXT,
    "resume_node": {"node_id", "hold_id", "expected_revision"},
    "block_node": {
        "node_id", "reason_key", "reason", "owner_kind", "owner_id",
        "condition", "ask_artifact_id", "expected_revision",
    } | _NEXT,
    "resolve_blocker": {"node_id", "hold_id", "expected_revision"},
    "request_review": {
        "node_id", "evidence", "artifact_ids", "expected_revision",
    } | _NEXT,
    "reject": {"node_id", "feedback", "expected_revision"},
    "fail": {"node_id", "evidence", "expected_revision"} | _NEXT,
    "attach_spawned_thread": {
        "node_id",
        "spawned_thread_id",
        "expected_revision",
    },
    "replace_spawned_thread": {
        "node_id",
        "spawned_thread_id",
        "expected_revision",
    },
    "accept": {
        "node_id", "verification_result", "evidence", "expected_revision",
    } | _NEXT,
    "accept_with_follow_up": {
        "node_id",
        "title",
        "acceptance_criteria",
        "size",
        "intent",
        "intent_words",
        "verification_result",
        "evidence",
        "expected_revision",
    } | _NEXT,
    "add_correction": {
        "node_id", "title", "acceptance_criteria", "affected_nodes",
        "intent", "intent_words", "expected_revision",
    },
    "arm_watch": {
        "node_id",
        "condition",
        "artifact_id",
        "expected_event",
        "reason",
        "expected_revision",
    } | _NEXT,
    "resolve_watch": {"node_id", "watch_id", "reason", "evidence", "expected_revision"},
}


OPERATION_REQUIRED_FIELDS = {
    "add": {"title", "acceptance_criteria"},
    "pause": {"expected_revision"},
    "resume": {"expected_revision"},
    "insert": {"title", "acceptance_criteria"},
    "edit": {"node_id", "expected_revision"},
    "split": {"node_id", "title", "acceptance_criteria", "size", "expected_revision"},
    "reorder": {"node_id", "order_key", "expected_revision"},
    "add_dependency": {"node_id", "depends_on_node_id", "expected_revision"},
    "remove_dependency": {"node_id", "depends_on_node_id", "expected_revision"},
    "add_follow_up": {
        "node_id", "title", "acceptance_criteria", "size", "expected_revision",
    },
    # Two valid bodies, which one required set cannot express: one node with
    # its revision, or ``node_ids`` alone. ``_validate_archive_target`` is the
    # enforcement and ``plan_operation_docs`` publishes the choice.
    "archive_node": set(),
    "close_node": {"node_id", "reason", "expected_revision"},
    "mark_done": {"node_id", "expected_revision"},
    "activate": {"node_id", "expected_revision"},
    "start": {"node_id", "expected_revision"},
    "pause_node": {"node_id", "reason", "expected_revision"},
    "resume_node": {"node_id", "hold_id", "expected_revision"},
    "block_node": {
        "node_id", "reason_key", "reason", "owner_kind", "expected_revision",
    },
    "resolve_blocker": {"node_id", "hold_id", "expected_revision"},
    "request_review": {
        "node_id", "evidence", "expected_revision",
    },
    "reject": {"node_id", "feedback", "expected_revision"},
    "fail": {"node_id", "expected_revision"},
    "attach_spawned_thread": {
        "node_id", "spawned_thread_id", "expected_revision",
    },
    "replace_spawned_thread": {
        "node_id", "spawned_thread_id", "expected_revision",
    },
    "accept": {"node_id", "expected_revision"},
    "accept_with_follow_up": {
        "node_id", "title", "acceptance_criteria", "size", "expected_revision",
    },
    "add_correction": {
        "node_id", "title", "acceptance_criteria", "affected_nodes",
        "expected_revision",
    },
    # expected_event, or a reason that schedules the task instead
    # (``plans.scheduled_wait``); ``boundary_after._park_write`` enforces it.
    "arm_watch": {"node_id", "condition", "expected_revision"},
    "resolve_watch": {"node_id", "watch_id", "expected_revision"},
}


#: How many nodes one bulk archive may retire. A cleanup pass is bounded so a
#: single call cannot walk an unbounded plan inside one transaction.
MAX_ARCHIVE_NODES = 200

_ARCHIVE_TARGET = (
    "archive_node requires node_id with its expected_revision, or node_ids to "
    "retire many nodes in one call"
)
_ARCHIVE_BULK_ALONE = (
    "archive_node with node_ids takes no node_id, expected_revision or "
    "next_node_id: a bulk archive has no single node those describe"
)
_ARCHIVE_IDS = (
    f"node_ids must be a list of 1 to {MAX_ARCHIVE_NODES} plan node ids"
)


def validate_operation_params(operation: str, params: dict) -> None:
    """Reject cross-operation fields and require revision evidence."""

    allowed = OPERATION_FIELDS.get(operation)
    if allowed is None:
        raise PlanInputError(f"unsupported plan operation: {operation}")
    extras = sorted(set(params) - allowed)
    if extras:
        joined = ", ".join(extras)
        raise PlanInputError(
            f"unexpected parameters for {operation}: {joined}"
        )
    if "next_node_id" in params:
        value = params["next_node_id"]
        if not isinstance(value, str) or not value.strip():
            raise PlanInputError("next_node_id must be a node id")
    if operation in {"add", "insert"}:
        return
    if operation == "archive_node":
        _validate_archive_target(params)
        return
    _require_revision(params)


def _validate_archive_target(params: dict) -> None:
    """Require exactly one archive shape: one node with evidence, or many ids.

    A bulk archive takes no position on any node's state -- archive never does
    -- so there is no single revision it could carry, and asking for one per id
    would make the cheap cleanup this exists for the expensive one it replaces.
    """
    # Presence, not value, picks the shape: dispatch routes on the key, so a
    # null `node_ids` must be refused here rather than reach the bulk writer.
    if "node_ids" not in params:
        if "node_id" not in params:
            raise PlanInputError(_ARCHIVE_TARGET)
        _require_revision(params)
        return
    if {"node_id", "expected_revision", "next_node_id"} & set(params):
        raise PlanInputError(_ARCHIVE_BULK_ALONE)
    ids = params["node_ids"]
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= MAX_ARCHIVE_NODES
        or any(not isinstance(value, str) or not value.strip() for value in ids)
    ):
        raise PlanInputError(_ARCHIVE_IDS)


def _require_revision(params: dict) -> None:
    """Require the node revision every single-node write is computed against."""
    if "expected_revision" not in params:
        raise PlanInputError("expected_revision is required")
    value = params["expected_revision"]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PlanInputError(
            "expected_revision must be a non-negative integer"
        )


def _user_step_description(params: dict[str, Any]) -> None:
    """Require a person-facing how-to and the agent's attempted route."""
    if params.get("kind", "task") != "task" or params.get("assignee") != "user":
        return
    description = params.get("description")
    if (
        not isinstance(description, str)
        or not description.strip()
        or USER_STEP_TRIED_MARKER.casefold() not in description.casefold()
        or not description.casefold().split(USER_STEP_TRIED_MARKER.casefold(), 1)[1].strip()
    ):
        raise PlanInputError(USER_STEP_DESCRIPTION_REFUSAL)
    if len(description) > PERSON_STEP_DESCRIPTION_LIMIT:
        raise PlanInputError(PERSON_STEP_DESCRIPTION_LIMIT_REFUSAL)


def node_fields(
    params: dict[str, Any], *, workflow_version: str | None = None,
    include_before: bool = True, intent_required: bool = True,
) -> dict[str, Any]:
    """Read common creation fields and reject absent durable intent.

    ``workflow_version`` is resolved by the server from the named workflow
    skill, never read from caller params.
    """
    _user_step_description(params)
    kind = params.get("kind", "task")
    missing_size = kind == "task" and params.get("size") is None
    missing_intent = kind == "task" and intent_required and params.get("intent") is None
    if missing_size and missing_intent:
        raise PlanInputError("task creation requires size and intent")
    if missing_intent:
        raise PlanInputError("task creation requires intent")
    size = require_task_size(params.get("size")) if kind == "task" else validate_size(params.get("size"))
    return {
        "kind": kind,
        "title": text(params, "title"),
        "acceptance_criteria": text(params, "acceptance_criteria"),
        "parent_id": params.get("parent_id"),
        "description": params.get("description"),
        "workflow_ref": params.get("workflow_ref"),
        "workflow_version": workflow_version,
        "workflow_step": params.get("workflow_step"),
        "difficulty": params.get("difficulty"),
        "size": size,
        "intent": params.get("intent"),
        "intent_words": params.get("intent_words"),
        "checkpoint_exception": bool(params.get("checkpoint_exception", False)),
        "assignee": params.get("assignee"),
        "spawn_kind": params.get("spawn_kind"),
        "watchdog_budget_seconds": params.get("watchdog_budget_seconds"),
        **({"before_node_id": params["before_node_id"]}
           if include_before and "before_node_id" in params else {}),
    }


def edit_fields(params: dict[str, Any]) -> dict[str, Any]:
    """Pass only intentionally supplied editable node fields and a re-rating."""
    fields: dict[str, Any] = {
        key: params[key]
        for key in (
            "title", "description", "acceptance_criteria", "intent", "intent_words", "watchdog_budget_seconds",
        )
        if key in params
    }
    if "size" in params:
        fields["size"] = validate_size(params["size"])
    if "difficulty_rerated" in params:
        from plans.workflow_fields import rerating

        fields["difficulty_rerated"] = rerating(params["difficulty_rerated"])
    if not fields:
        raise PlanInputError("edit requires at least one changed field")
    return fields


def expected(params: dict[str, Any]) -> dict[str, int]:
    """Return revision evidence already validated at the mutation boundary."""
    return {"expected_revision": params["expected_revision"]}


def require_passed_verification(params: dict[str, Any]) -> None:
    """Default an omitted successful verification and refuse every other value."""
    if params.get("verification_result", "passed") != "passed":
        raise PlanInputError("verification_result must be 'passed' when supplied")


def text(params: dict[str, Any], field: str) -> str:
    """Return one required textual argument."""
    value = params.get(field)
    if not isinstance(value, str) or not value.strip():
        raise PlanInputError(f"{field} is required")
    return value.strip()


def integer(params: dict[str, Any], field: str) -> int:
    """Return one required integer argument without accepting booleans."""
    value = params.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanInputError(f"{field} must be an integer")
    return value
