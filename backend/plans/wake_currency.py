"""Whether a plan wake is still worth a Turn.

A result-bearing wake is news plus the facts to act on it. If the thread it
names has nothing it could act on in that plan, a Turn started for it reads the
news and stops, which is the cost this whole recovery exists to remove.

One predicate answers that twice: when the wake is appended, so a pointless one
is never queued, and again when a Turn would claim it, so one that went stale
while it waited never starts a Turn either.

A wake about a delegated step's worker names that step and that worker. The
step reads as waiting on its worker while the worker works (matrix row D4), so
it is not work liveness would count; the wake *is* the parent's reason to act.
The same holds for the worker handed a rejected step's revision: it revises in
its own plan, whatever the parent's executor slot holds. Either wake stays
current while the step is open, carries no hold (a paused or blocked step is
not worth a Turn), and still names that worker: a wake queued by a worker that
was since replaced or released is not about the step any more.
"""

from __future__ import annotations

from plans.plan_wake import CONDUCTOR_TASK_SETTLED

#: The continuation kind a result-bearing plan wake carries.
PLAN_WAKE = "plan_wake"
#: The kind a pause carries into a running Turn (pause contract rule 1, #1970).
PAUSE_STEER = "plan_pause_steer"
#: Every kind whose currency Plans answers.
KINDS = frozenset({PLAN_WAKE, PAUSE_STEER})
#: Wakes that report an outcome the recipient asked to hear about another
#: plan's task (#488). They are the recipient's reason to act, so they stay
#: current while its plan is active even when it holds no open work.
_NEWS_WAKES = frozenset({CONDUCTOR_TASK_SETTLED})


def condition(
    *, plan_id: str, kind: str, subject_id: str, revision: int,
    delegated_step_id: str | None = None,
    delegated_worker: str | None = None,
    open_node_id: str | None = None,
) -> dict:
    """Return the currency condition one wake travels with."""
    details = {"plan_id": plan_id}
    if delegated_step_id is not None:
        details["delegated_step_id"] = delegated_step_id
    if delegated_worker is not None:
        details["delegated_worker"] = delegated_worker
    if open_node_id is not None:
        details["open_node_id"] = open_node_id
    return {
        "kind": PLAN_WAKE,
        "subject_id": subject_id,
        "transition": f"{kind}:{revision}",
        "details": details,
    }


def pause_condition(*, plan_id: str, revision: int) -> dict:
    """Return the currency condition a pause travels into a running Turn with."""
    return {
        "kind": PAUSE_STEER,
        "subject_id": plan_id,
        "transition": f"paused:{revision}",
        "details": {"plan_id": plan_id},
    }


def is_current(thread_id: str, condition: dict, *, db) -> bool:
    """Return whether this thread still has work it could act on in that plan.

    Asked of the same predicate liveness uses, so a wake is never refused for
    work a liveness nudge would have counted. That matters most for a review
    this thread owns: it is actionable, and a rule built on open *tasks* alone
    would drop the wake that reports it.

    A pause is current only while the plan is still paused and a Turn is
    running to hear it. It reaches that Turn at a safe boundary; a claim that
    would *start* a Turn finds none running and cancels it, so a pause never
    starts the agent it paused (rule 2).
    """
    from plans import projections

    details = condition.get("details")
    if not isinstance(details, dict):
        return False
    plan_id = details.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id:
        return False
    if condition.get("kind") == PAUSE_STEER:
        return _pause_heard(db, thread_id, plan_id)
    plan = db.execute(
        "SELECT * FROM plans WHERE id=? AND state='active'", (plan_id,),
    ).fetchone()
    if plan is None:
        return False
    if str(condition.get("transition") or "").partition(":")[0] in _NEWS_WAKES:
        return True
    step_id = details.get("delegated_step_id")
    worker = details.get("delegated_worker")
    if (
        isinstance(step_id, str) and isinstance(worker, str)
        and _step_open(db, plan_id, step_id, worker)
    ):
        return True
    # A wake about one task of this plan -- one blocked for good behind
    # another plan's failed task -- is the owner's to act on while that task
    # is open, even though a blocked task is not actionable work (#1552).
    node_id = details.get("open_node_id")
    if isinstance(node_id, str) and _node_open(db, plan_id, node_id):
        return True
    actionable, _projected = projections.actionable_leaves(
        db, plan, executor=thread_id,
    )
    return bool(actionable)


def step_news_queued(db, thread_id: str, step_id: str) -> bool:
    """Return whether a wake about this delegated step still waits in the thread's FIFO."""
    return db.execute(
        """SELECT 1 FROM messages
            WHERE thread_id=? AND delivery_state='queued' AND input_order IS NOT NULL
              AND json_extract(continuation_condition,'$.details.delegated_step_id')=?
            LIMIT 1""",
        (thread_id, step_id),
    ).fetchone() is not None


def _pause_heard(db, thread_id: str, plan_id: str) -> bool:
    """Whether that plan is still paused and this thread has a Turn running."""
    return db.execute(
        """SELECT 1 FROM plans p JOIN active_turns a ON a.thread_id=?
           WHERE p.id=? AND p.state='paused' AND a.status='running'""",
        (thread_id, plan_id),
    ).fetchone() is not None


def _node_open(db, plan_id: str, node_id: str) -> bool:
    """Whether that task is still open and unarchived in that plan."""
    from plans.node_states import _TERMINAL_STATES, in_work_sql

    row = db.execute(
        f"SELECT state FROM plan_nodes AS n WHERE id=? AND plan_id=? AND {in_work_sql('n')}",
        (node_id, plan_id),
    ).fetchone()
    return row is not None and row["state"] not in _TERMINAL_STATES


def _step_open(db, plan_id: str, step_id: str, worker: str) -> bool:
    """Whether the step still awaits an outcome, unheld, from the worker the wake names."""
    from plans import holds, projections

    step = projections.delegated_step(db, plan_id, worker)
    return (
        step is not None and step["id"] == step_id
        and not holds.active_holds(step_id, db=db)
    )
