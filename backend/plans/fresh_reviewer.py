"""Who may settle a reviewer step: a worker that has seen none of the work it checks.

A reviewer step (``by: reviewer``) is settled from the return of a worker started
for that step. The step's lineage is the step and its send-back revisions: the
nodes under the same parent with the same workflow step, so the same reviewer
may take the next attempt. The work the step checks is its run, the outer run
when the run is itself a step, or the task the run serves. A worker that was
handed or executed any other node there has seen the
work, and its return can't settle the step. Only an agent is held to this; the
person settles any step.
"""

from __future__ import annotations

import plans.service as service

NOT_FRESH = (
    "This review can't settle the step: the worker that returned it ({worker}) already worked on this task. "
    "Hand the step to a new worker (delegate_task with plan_node_id and no thread of this task), "
    "or ask the person to settle it."
)
AUTHOR_FAIL = (
    "Only the reviewer's verdict can fail this step. To act on a failing review, send it back "
    "(reject with feedback) and restart it once the work is fixed. To drop the work, ask the person."
)


def _work_root(db, step) -> str:
    """The node whose subtree is the work ``step`` checks."""
    node = service._node_row(db, step["parent_id"])
    while node["workflow_step"] is not None and node["parent_id"] is not None:
        node = service._node_row(db, node["parent_id"])
    if node["parent_id"] is not None:
        serves = service._node_row(db, node["parent_id"])
        if serves["kind"] == "task":
            return str(serves["id"])
    return str(node["id"])


def has_seen_the_work(db, step, worker: str) -> bool:
    """Whether ``worker`` worked on a node of ``step``'s work outside the step's lineage.

    The plan's own thread is never a worker here: attaching a thread to a node requires
    the plan's owner to have spawned it.
    """
    return db.execute(
        "WITH RECURSIVE work(id) AS ("
        " SELECT :root UNION ALL SELECT n.id FROM plan_nodes n JOIN work ON n.parent_id=work.id)"
        " SELECT 1 FROM plan_nodes n JOIN work ON n.id=work.id"
        " WHERE NOT (COALESCE(n.parent_id,'')=:parent AND COALESCE(n.workflow_step,'')=:step)"
        " AND (n.spawned_thread_id=:w OR n.executor_thread_id=:w"
        "  OR EXISTS (SELECT 1 FROM plan_node_attempts a WHERE a.node_id=n.id AND a.executor_thread_id=:w)"
        "  OR EXISTS (SELECT 1 FROM plan_node_events e WHERE e.node_id=n.id"
        "   AND e.event='spawned_thread_attached' AND json_valid(e.payload_json)"
        "   AND json_extract(e.payload_json,'$.spawned_thread_id')=:w))"
        " LIMIT 1",
        {"root": _work_root(db, step), "parent": step["parent_id"], "step": step["workflow_step"], "w": worker},
    ).fetchone() is not None


def refuse_unfresh(db, step) -> None:
    """Refuse settling ``step`` from a return by a worker that has seen the work."""
    worker = step["spawned_thread_id"]
    if worker and has_seen_the_work(db, step, str(worker)):
        raise service.PlanError(NOT_FRESH.format(worker=worker))
