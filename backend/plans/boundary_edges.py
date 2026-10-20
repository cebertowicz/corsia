"""Graph and lineage edges for the plan mutation boundary: follow-ups,
archives and dependencies, each routed to its existing owner."""

from __future__ import annotations

from typing import Any

import plans.admission as admission
import plans.boundary_after as after
import plans.boundary_fields as fields
import plans.service as service

EDGE_OPERATIONS = frozenset({
    "add_follow_up", "archive_node", "add_dependency", "remove_dependency",
})


def dispatch(
    plan_id: str, operation: str, params: dict[str, Any], actor, common: dict, db,
):
    """Route graph and lineage writers to their existing owners."""
    if operation == "add_follow_up":
        import plans.followups as followups

        created = followups.add_follow_up(
            params["node_id"], title=fields.text(params, "title"),
            acceptance_criteria=fields.text(params, "acceptance_criteria"),
            size=fields.require_task_size(params.get("size")),
            **fields.expected(params), **common,
        )
        return service.get_node(params["node_id"], db=db), created
    if operation == "archive_node":
        import plans.archival as archival

        if "node_ids" in params:
            # A bulk archive describes no single node, so it returns its own
            # per-node report rather than a node projection or a next task.
            return after.archive_many(
                plan_id, params["node_ids"], common, db,
                executor=actor.thread_id if actor.kind == "thread" else None,
            )
        archival.archive_node(
            params["node_id"], **fields.expected(params), **common,
        )
        return after.after_close(
            params["node_id"], "archived", params, common,
            executor=actor.thread_id if actor.kind == "thread" else None,
        )
    import plans.dependencies as dependencies

    call = (
        dependencies.add_dependency if operation == "add_dependency"
        else dependencies.remove_dependency
    )
    outcome = call(
        params["node_id"], fields.text(params, "depends_on_node_id"),
        causing_executor=actor.thread_id if actor.kind == "thread" else None,
        **fields.expected(params), **common,
    )

    result = admission.result(params["node_id"], db=db)
    if operation == "remove_dependency":
        # An unchanged graph looks the same to a retry whether it or the lost
        # first call emptied it; this says which.
        result["removed"] = bool(outcome)
    return result
