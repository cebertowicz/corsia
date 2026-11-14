"""Authorization for a coordinator's constrained foreign plan writes."""

from __future__ import annotations

from typing import Any

import infra.conductor as conductor
from infra.corsia_agent import get_corsia_agent

#: A coordinator created this task on another agent's plan. The creation
#: event's provenance is the durable receipt ``cross_plan`` reads to tell the
#: coordinator once the task settles, so stored rows keep this exact string.
ADD_PROVENANCE = "plan.conductor_add"
#: A coordinator paused, resumed or re-ordered another agent's plan. The
#: boundary stamps it only after authorizing that coordinator, and
#: ``pause_authority`` reads it as that authorization.
STEER_PROVENANCE = "plan.coordinator_steer"


def coordinator_kind(db, plan, actor_id: str | None) -> str | None:
    """Which coordinator of the plan's owner ``actor_id`` is: ``workspace``, ``project`` or none.

    The one rule for steering another agent's plan. ``coordinator_may_steer``
    authorizes with it, and an add records its answer at creation, before
    designations change.
    """
    if not actor_id or plan["owner_kind"] != "thread":
        return None
    owner = db.execute(
        "SELECT project_id FROM threads WHERE id=?", (plan["owner_id"],)
    ).fetchone()
    if owner is None:
        return None
    if actor_id == get_corsia_agent(db=db):
        return "workspace"
    if owner["project_id"] is not None and actor_id == conductor.get_scope_coordinator(
            str(owner["project_id"]), db):
        return "project"
    return None


def coordinator_may_steer(db, plan, actor) -> bool:
    """Whether ``actor`` is the target project's or workspace's conductor."""
    return actor.kind == "thread" and coordinator_kind(db, plan, actor.thread_id) is not None


def thread_may_mutate(db, plan, operation: str, params: dict[str, Any], actor) -> bool:
    """Apply ownership and the coordinator's top-level steering authority."""
    if plan["owner_kind"] != "thread" or plan["owner_id"] == actor.thread_id:
        return True
    if not coordinator_may_steer(db, plan, actor):
        return False
    if operation in {"pause", "resume"}:
        return True
    if operation in {"add", "insert"}:
        if any(params.get(field) is not None for field in
               ("parent_id", "depends_on_node_id", "next_node_id")):
            return False
        if operation == "add":
            return True
        target = params.get("before_node_id")
    elif operation == "reorder":
        target = params.get("node_id")
    else:
        return False
    if not isinstance(target, str) or not target:
        return False
    node = db.execute("SELECT plan_id, parent_id FROM plan_nodes WHERE id=?", (target,)).fetchone()
    return node is not None and node["plan_id"] == plan["id"] and node["parent_id"] is None
