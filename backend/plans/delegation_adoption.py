"""Take over a worker whose delegator was replaced off its step (grandchild adoption).

When a mid-tier worker (the delegator) is replaced while a worker of its own
(the grandchild) still runs, that grandchild keeps naming the delegator as its
spawner and reports to nobody the grandparent can reach. The replace result
names it; this module is what lets the grandparent act on that.

Deleting or purging a delegator is not an adoption source (#2647, owner,
2026-09-28 "delete means delete"): its temporary workers are deleted with it
and its promoted agents become top-level, so nothing is left to adopt.

Attaching a thread its owner did not spawn is refused everywhere else. The one
exception is an orphan: its spawner was replaced off a step of the adopting
plan, and that spawner was itself delegated to by a step of the adopting plan,
so the grandchild sits in the adopter's own delegation subtree.
Adopting re-points ``threads.spawned_by`` to the adopting plan's owner and
re-derives the grandchild plan's parent from it, in the attach's own write.
Every wake, epoch check and decision review that reads either then agrees.
Nothing is adopted automatically: the grandparent chooses, with
``replace_spawned_thread`` or ``attach_spawned_thread``.
"""

from __future__ import annotations

import json

import plans.service as service

#: Detach reason ``delegation_replace`` records when a step moves to another worker.
_REPLACED = "replaced"


def _attachment_history(connection, delegator_id: str) -> list:
    """Return every attach and detach of this delegator on any step, newest first."""
    return connection.execute(
        """SELECT e.event, n.plan_id, e.payload_json
           FROM plan_node_events e JOIN plan_nodes n ON n.id = e.node_id
           WHERE e.event IN ('spawned_thread_attached','spawned_thread_detached')
             AND json_extract(e.payload_json, '$.spawned_thread_id') = ?
           ORDER BY e.created_at DESC, e.rowid DESC""",
        (delegator_id,),
    ).fetchall()


def _was_replaced(history: list) -> bool:
    """Whether the delegator's latest attachment change was being replaced off a step."""
    if not history or history[0]["event"] != "spawned_thread_detached":
        return False
    payload = json.loads(history[0]["payload_json"] or "{}")
    return payload.get("reason") == _REPLACED


def require_orphan(connection, plan, child_id: str, delegator_id: str | None) -> None:
    """Refuse an adoption unless the child's spawner was replaced off our step.

    Raises :class:`plans.errors.PlanError` saying which condition failed. Called
    only for a thread whose ``spawned_by`` is not the plan's owner.
    """
    refusal = f"spawned thread {child_id} is not owned by this plan node"
    if not delegator_id:
        raise service.PlanError(f"{refusal}: it has no spawner to adopt it from")
    history = _attachment_history(connection, delegator_id)
    last_attached = next(
        (row for row in history if row["event"] == "spawned_thread_attached"), None,
    )
    if last_attached is None or last_attached["plan_id"] != plan["id"]:
        raise service.PlanError(
            f"{refusal}: it was spawned by {delegator_id}, which no step of this plan"
            " delegated to, so it is outside this plan's delegation and cannot be adopted"
        )
    if not _was_replaced(history):
        raise service.PlanError(
            f"{refusal}: it was spawned by {delegator_id}, which was not replaced"
            " off its step: a worker can be adopted only once replace_spawned_thread"
            " has moved its delegator's step to another worker"
        )


def adopt_orphan(connection, plan, child_id: str) -> None:
    """Re-point an orphan to this plan's owner and its plan under this plan.

    A thread this plan's owner already spawned is left alone; anything else
    reaching here passed ``require_orphan``. Runs inside the attach's write, so
    a later refusal of the attach undoes the re-pointing too.
    """
    from core.thread_identity import repoint_spawned_by

    row = connection.execute(
        "SELECT spawned_by FROM threads WHERE id = ?", (child_id,),
    ).fetchone()
    if row["spawned_by"] == plan["owner_id"]:
        return
    repoint_spawned_by(connection, child_id, plan["owner_id"])
