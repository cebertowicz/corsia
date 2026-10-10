"""Block a task on someone other than the user, the way ``block_node`` does.

A blocker anyone but the user clears is a wait with a deadline
(``plans.blocked_wait``), armed through the writer ``block_node`` uses, so
tests reach the same rows without a Turn or a tool call.
"""

from __future__ import annotations

from plans import blocked_wait, service, watch_operations
from plans.boundary import PlanActor

OWNER = PlanActor("owner", "user", "project")


def wait_on(
    db, node_id: str, *, reason: str, owner_kind: str = "external",
    reason_key: str = "test_wait", actor: PlanActor | None = None,
):
    """Arm a blocked wait on one task and return the watch."""
    node = service._node_row(db, node_id)
    params = blocked_wait.watch_params({
        "node_id": node_id, "reason_key": reason_key, "reason": reason,
        "owner_kind": owner_kind, "expected_revision": node["revision"],
    })
    with service._write(db):
        return watch_operations.arm_write(
            str(node["plan_id"]), params, actor or OWNER, db,
        )
