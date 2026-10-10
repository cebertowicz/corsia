"""What a delegation's two threads may not do to the worker's plan (matrix section G).

The worker settles its own steps and the parent judges what comes back, on
its own delegated step. So a parent that reaches into its worker's plan is
told where its verdict goes (G7), and a worker never assigns a step to the
user, because what reaches the user is the parent's call (G11). Both are
refused before any write, with the step to take instead.
"""

from __future__ import annotations

from typing import Any

import plans.errors as errors
from prompts import plan_tools as plan_text


def authorize(db, plan, operation: str, params: dict[str, Any], actor) -> None:
    """Refuse a parent driving its worker's plan, or a worker adding a user step."""
    if actor.kind != "thread" or plan["owner_kind"] != "thread":
        return
    from plans import projections

    worker, parent_plan_id = str(plan["owner_id"]), plan["parent_plan_id"]
    if not projections.live_delegation(db, worker, parent_plan_id):
        return
    if actor.thread_id != worker:
        parent = db.execute(
            "SELECT owner_id FROM plans WHERE id=?", (parent_plan_id,),
        ).fetchone()
        if parent is not None and parent["owner_id"] == actor.thread_id:
            step = projections.delegated_step(db, parent_plan_id, worker)
            raise errors.PlanInputError(
                plan_text.workers_own_step(step["title"], parent_plan_id, step["id"]),
            )
        return
    if operation in {"add", "insert"} and params.get("assignee") == "user":
        raise errors.PlanInputError(plan_text.WORKER_STEP_TO_USER)
