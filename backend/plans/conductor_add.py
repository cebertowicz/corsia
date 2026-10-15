"""Authorization for the one foreign write a conductor may make (#488)."""

from __future__ import annotations

from typing import Any

import infra.conductor as conductor
from infra.corsia_agent import get_corsia_agent


def conductor_may_add(db, plan, actor) -> bool:
    """Whether ``actor`` is the target project's or workspace's conductor."""
    if actor.kind != "thread" or actor.thread_id is None or plan["owner_kind"] != "thread":
        return False
    owner = db.execute(
        "SELECT project_id FROM threads WHERE id=?", (plan["owner_id"],)
    ).fetchone()
    if owner is None:
        return False
    return actor.thread_id in {
        conductor.get_scope_coordinator(str(owner["project_id"]), db),
        get_corsia_agent(db=db),
    }


def thread_may_mutate(db, plan, operation: str, params: dict[str, Any], actor) -> bool:
    """Apply thread-plan ownership, plus the conductor's constrained append."""
    if plan["owner_kind"] != "thread" or plan["owner_id"] == actor.thread_id:
        return True
    return (
        operation == "add"
        and all(
            params.get(field) is None
            for field in ("parent_id", "depends_on_node_id", "next_node_id")
        )
        and conductor_may_add(db, plan, actor)
    )
