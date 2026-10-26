"""Where a delegated child's model calls are billed when it holds no selection.

A call is billed to the executor's valid selection (``admission_origin.turn_snapshot``).
A delegated child spends many Turns holding none: a step it started but parked
behind an armed watch is active without a slot, and before its first step (or
for a child that never plans) it has nothing selected at all. A webhook,
reviewer or message wakes such a Turn and its calls were left unbound, so the
parent's task cost did not carry them.

This module answers only the usage question; it never writes ``active_turns``
and never selects, so admission, liveness and the working label are unchanged.

1. One open active step of the child's own plan, parked rather than eligible:
   the calls belong to that step, on its latest attempt (``plan_usage`` requires
   an attempt) and its own commitment.
2. Otherwise, while the child has no active, returned or finished step of its
   own and the step it was handed is still pending or active: the calls belong
   to that handed step, as ``delegation`` on the parent's commitment.

Anything else stays unbound on purpose: work already returned for review or
finished is not the task's work, and more than one candidate is not guessed.
"""

from __future__ import annotations

import plans.selection_validity as validity
from plans import commitments, node_states

_BILLABLE_BLOCKERS = (None, validity.HELD)
#: A handed step the child is still working: not yet returned for review.
_HANDED_STATES = ("pending", "active")
_PAST_WORK_STATES = ("review_requested", *sorted(node_states._TERMINAL_STATES))


def binding(executor: str, *, db) -> dict | None:
    """Return the plan fields the executor's calls now belong to, or None."""
    plan = db.execute(
        "SELECT * FROM plans WHERE owner_kind='thread' AND owner_id=?", (executor,),
    ).fetchone()
    if plan is not None:
        own = _own_active_step(db, plan)
        if own == "ambiguous":
            return None
        if own is not None:
            return _step_binding(db, plan, own, executor)
        if _has_past_work(db, plan):
            return None
    return _handed_binding(db, executor)


def _own_active_step(db, plan):
    """Return the plan's one active step that is parked or eligible, 'ambiguous', or None."""
    rows = db.execute(
        """SELECT * FROM plan_nodes WHERE plan_id=? AND kind='task'
           AND state='active' AND archived_at IS NULL ORDER BY id""",
        (plan["id"],),
    ).fetchall()
    blockers = validity.slot_blockers(db, [(row, plan) for row in rows])
    billable = [row for row in rows if blockers[str(row["id"])] in _BILLABLE_BLOCKERS]
    if len(billable) > 1:
        return "ambiguous"
    return billable[0] if billable else None


def _has_past_work(db, plan) -> bool:
    marks = ",".join("?" for _ in _PAST_WORK_STATES)
    return db.execute(
        f"SELECT 1 FROM plan_nodes WHERE plan_id=? AND kind='task' AND state IN ({marks}) LIMIT 1",
        (plan["id"], *_PAST_WORK_STATES),
    ).fetchone() is not None


def _step_binding(db, plan, node, executor: str) -> dict | None:
    attempt = db.execute(
        """SELECT id FROM plan_node_attempts WHERE node_id=? AND executor_thread_id=?
           ORDER BY selected_at DESC,id DESC LIMIT 1""",
        (node["id"], executor),
    ).fetchone()
    if attempt is None:
        return None
    return _fields(plan["id"], node, attempt["id"], commitments.usage_role(str(node["id"]), db=db))


def _handed_binding(db, executor: str) -> dict | None:
    marks = ",".join("?" for _ in _HANDED_STATES)
    node = db.execute(
        f"""SELECT * FROM plan_nodes WHERE spawned_thread_id=? AND archived_at IS NULL
            AND state IN ({marks}) ORDER BY id LIMIT 1""",
        (executor, *_HANDED_STATES),
    ).fetchone()
    if node is None:
        return None
    attempt = db.execute(
        "SELECT id FROM plan_node_attempts WHERE node_id=? ORDER BY selected_at DESC,id DESC LIMIT 1",
        (node["id"],),
    ).fetchone()
    if attempt is None:
        return None
    return _fields(node["plan_id"], node, attempt["id"], "delegation")


def _fields(plan_id, node, attempt_id, role: str) -> dict | None:
    if node["commitment_id"] is None:
        return None
    return {
        "plan_id": plan_id,
        "plan_node_id": node["id"],
        "plan_attempt_id": attempt_id,
        "plan_commitment_id": node["commitment_id"],
        "plan_usage_role": role,
    }


def stamp_running_turn(db, executor: str) -> None:
    """Bill the calls a just-attached child already made this Turn, if still unbound.

    A worker can start its first Turn before the hand-over commits, so its
    first calls were recorded with nothing to bind them. Called inside the
    attach write, it gives only the Turn's own unbound rows and pending
    receipts the binding later calls will get; it changes no ``active_turns``
    snapshot and no row that already names a task.
    """
    from infra.turn_usage_receipts import attribute_unbound_turn_usage
    from plans import admission_attempts

    turn = admission_attempts.turn_binding(db, executor)
    if turn is None or turn["plan_attempt_id"] is not None:
        return
    fields = binding(executor, db=db)
    if fields is not None:
        attribute_unbound_turn_usage(
            db, thread_id=executor, turn_id=str(turn["id"]), binding=fields,
        )
