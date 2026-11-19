"""Who may settle a reviewer step: a worker that has seen none of the work it checks.

A reviewer step (``by: reviewer``) is settled from the return of a worker started
for that step. The step's lineage is the step and its send-back revisions: the
nodes under the same parent with the same workflow step, so the same reviewer
may take the next attempt. The work the step checks is its run, or the outer
run when the run is itself a step; a run never sits under a task, since a step
holds tasks only. A worker that was handed or executed any other node there has
seen the work, and its return can't settle the step. Only an agent is held to
this; the person settles any step.
"""

from __future__ import annotations

import plans.service as service

NOT_FRESH = (
    "This review can't settle the step: the worker that returned it ({worker}) already worked on this task. "
    "Hand the step to a new worker: start one with delegate_task (plan_node_id='none'), "
    "then plan_update replace_spawned_thread on this step."
)
AUTHOR_FAIL = (
    "An agent can't fail a review step: a worker that has not seen the work checks it. "
    "While the step is open, start that worker on it with delegate_task. Once its review is back, "
    "accept it, or fix the work first and then reject with feedback, which sends it back to that reviewer."
)
#: The child_step_failed wake when the delegated step is a review step, which no agent
#: fails: only its reviewer's verdict settles it.
REVIEW_STEP_CHILD_FAILED_TEMPLATE = (
    "[CORSIA plan] A step of the review delegated for {title} failed, and the"
    " reviewer stopped with other work still open, so no verdict is coming yet."
    " Decide whether to retry it, re-scope it, or hand the step to a new"
    " reviewer: start one with delegate_task (plan_node_id='none'), then"
    " plan_update replace_spawned_thread on the step. A review step is settled"
    " by its reviewer's verdict, never failed. If the reviewer only replaced"
    " that step, nothing failed: have it archive the failed step, and"
    " close_node, not fail, any step a new brief replaces."
)


def _work_root(db, step) -> str:
    """The node whose subtree is the work ``step`` checks."""
    node = service._node_row(db, step["parent_id"])
    while node["workflow_step"] is not None and node["parent_id"] is not None:
        node = service._node_row(db, node["parent_id"])
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
