"""Where a delegated child's model calls are billed when it holds no selection.

A call is billed to the executor's valid selection (``admission_origin.turn_snapshot``).
A delegated child spends many Turns holding none: a step it started but parked
behind an armed watch is active without a slot, and before its first step (or
for a child that never plans) it has nothing selected at all. A webhook,
reviewer or message wakes such a Turn and its calls were left unbound, so the
parent's task cost did not carry them.

The step handed to the child is the canonical live attachment
(``commitments.attached_parent_node``). It raises on an ambiguous one on
purpose; that is left to ``usage_calls.binding_at_call_start``, which logs it,
records the call ``UNATTRIBUTED`` and reports the defect. Only while it is still pending or active is the child doing its work. Then:

1. A child step that is active and parked rather than eligible owns the calls,
   on its latest attempt (``plan_usage`` requires an attempt); two such steps
   are not guessed between.
2. Otherwise the handed step owns them, as ``delegation``.

Anything else stays unbound: work returned for review or finished is not the
task's work, and a thread nothing was handed to is not a delegated child.

This answers only the usage question; it never writes ``active_turns`` or a
selection. ``infra.turn_plan_binding`` still owns the Turn's snapshot (which
drives admission and the working label), so for these Turns cost follows the
step above while the snapshot, registrations and terminal-total fallback stay
unbound.
"""

from __future__ import annotations

import plans.selection_validity as validity
from plans import commitments

_BILLABLE_BLOCKERS = (None, validity.HELD)
#: A handed step the child is still working: not yet returned for review.
_HANDED_STATES = ("pending", "active")


def binding(executor: str, *, db) -> dict | None:
    """Return the plan fields the executor's calls now belong to, or None."""
    plan = db.execute(
        "SELECT * FROM plans WHERE owner_kind='thread' AND owner_id=?", (executor,),
    ).fetchone()
    if plan is None:
        return None
    handed = commitments.attached_parent_node(plan["id"], db=db)
    if handed is None or handed["state"] not in _HANDED_STATES:
        return None
    own = _own_parked_step(db, plan)
    if own == "ambiguous":
        return None
    if own is not None:
        return _bound(db, own, executor, commitments.usage_role(str(own["id"]), db=db))
    return _bound(db, handed, None, "delegation")


def _own_parked_step(db, plan):
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


def _bound(db, node, executor: str | None, role: str) -> dict | None:
    """The node's fields on its latest attempt (the executor's own, when named)."""
    if node["commitment_id"] is None:
        return None
    where = "node_id=?"
    params: tuple = (node["id"],)
    if executor is not None:
        where, params = "node_id=? AND executor_thread_id=?", (node["id"], executor)
    attempt = db.execute(
        f"SELECT id FROM plan_node_attempts WHERE {where} ORDER BY selected_at DESC,id DESC LIMIT 1",
        params,
    ).fetchone()
    if attempt is None:
        return None
    return {
        "plan_id": node["plan_id"],
        "plan_node_id": node["id"],
        "plan_attempt_id": attempt["id"],
        "plan_commitment_id": node["commitment_id"],
        "plan_usage_role": role,
    }
