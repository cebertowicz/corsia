"""Lifecycle consequences for outstanding decisions.

A decision is first-class *user attention*, so it outlives the agent that asked
it — but it cannot outlive the thing its answer settles. ``decisions.answer``
settles a decision by transitioning its user review node inside the owning plan,
and that plan must be live and readable for the answer to mean anything.

So exactly one rule decides survival:

* the **author/source** thread archiving changes nothing — the user still owes
  an answer and it still lands on the plan (PRD-PLANS E4, "never strand");
* the **owning plan** going away withdraws the question, because there is no
  longer anywhere for the answer to land;
* the **source task** closing withdraws it too: a closed task has left the
  work every decision operation authorizes against, so the question could
  otherwise never change again (#2651). An accepted or failed task is still
  work, and its question stays answerable.

Withdrawal reuses the existing terminal state (``invalidated``): attention
cleared, review node closed (a withdrawn question is not a failure, #2589), one
revisioned write, audit preserved. No new state, table, or queue.

This is the *write* side of the invariant "every counted queue item is
actionable". ``plans.attention_detail._answerable`` is the read side, so a row
that escaped this hook can still never become a head that starves later
decisions. Thread archive and purge, and a source task's close
(``plans.worker_retirement.settle_terminal``), are the lifecycle transactions
with a withdrawal hook. Source-node archival needs none: the boundary refuses to
archive a node whose decision question is still open.
"""

from __future__ import annotations

import plans.closure as closure
import plans.decision_projection as decision_projection
import plans.node_states as node_states
import plans.service as service
from registry import attention, models

#: Decisions in these states hold a place in the user's queue, or are about to.
_OPEN = ("draft", "parent_reviewed", "raised")

_ACTIVE_DECISIONS_SQL = """
    SELECT i.id AS artifact_id, i.external_id, i.metadata, i.created_at,
           l.node_id, n.plan_id
      FROM plan_node_artifact_links l
      JOIN artifact_identities i ON i.id = l.artifact_id
      JOIN plan_nodes n ON n.id = l.node_id
      JOIN plans p ON p.id = n.plan_id
     WHERE l.role = 'attention'
       AND i.integration = 'local'
       AND i.type = ?
       AND i.state = 'active'
"""

#: A plan whose owner thread is no longer live cannot be read, so its decisions
#: can never be settled. Mirrors ``intake._authorize_thread_plan``.
_OWNED_BY_THREAD_SQL = _ACTIVE_DECISIONS_SQL + """
       AND p.owner_kind = 'thread'
       AND p.owner_id = ?
"""

#: The questions drafted from one task, which always share its plan.
_FROM_SOURCE_SQL = _ACTIVE_DECISIONS_SQL + """
       AND n.plan_id = ?
       AND json_extract(i.metadata, '$.plan_decision.source_node_id') = ?
"""

#: Why a question left the queue when the task it was about closed.
SOURCE_CLOSED = "The task this decision was about was closed"
#: Why a question left the queue when the user marked its task done.
SOURCE_DONE = "The user marked the task this decision was about done"


#: The close reason every withdrawn decision's review node records.
WITHDRAWN = "Decision withdrawn"


def _close_review_node(db, node_id: str, *, actor_id: str, provenance: str) -> None:
    """Close the review node, tolerating one that is already terminal.

    The boundary now refuses generic settlement of a lane-owned node, so this
    only covers rows that predate that rule. Withdrawal must still be total: a
    thread the user archives can never be left unarchivable because some earlier
    operation ended the node first.

    The close's guards cannot refuse here. The review node is a task, and a
    task holds no children. While the question is open, the boundary refuses
    attach, replace and reject on it, so it never names a worker.
    """
    node = service._node_row(db, node_id)
    if node["state"] in service._TERMINAL_STATES or node["archived_at"] is not None:
        return
    closure.close_node(
        node_id, reason=WITHDRAWN, actor_id=actor_id, provenance=provenance, db=db,
    )


def _withdraw(
    db, row, *, reason: str, actor_id: str, provenance: str, readmit: bool = True,
) -> bool:
    """Settle one outstanding decision terminally. Returns whether it changed.

    ``readmit=False`` skips re-admitting the plan when the only task the
    question held, its source, is itself the one closing.
    """
    body = decision_projection._body(row)
    if body is None or body["state"] not in _OPEN:
        return False

    if body["state"] == decision_projection.RAISED:
        attention.clear_attention(
            row["artifact_id"], outcome="dismiss", reason=reason,
            db=db,
        )
    _close_review_node(db, body["node_id"], actor_id=actor_id, provenance=provenance)
    models.update_metadata(
        "local", decision_projection.DECISION_TYPE, row["external_id"],
        {"plan_decision": {
            **body,
            "state": "invalidated",
            "raised_at": None,
            "revision": body["revision"] + 1,
        }},
        db=db,
    )
    service._event(
        db, body["node_id"], "decision_invalidated", actor_id=actor_id,
        provenance=provenance, payload={"reason": reason},
    )
    if not readmit:
        return True
    # Withdrawing the question ends the wait it placed on its source task, so
    # the plan is re-admitted inside the archive's own write.
    from plans import admission

    admission.reconcile_plan(
        body["plan_id"], actor_id=actor_id, provenance=provenance, db=db,
    )
    return True


def withdraw_for_owner_thread_in_transaction(
    db, thread_id: str, *, reason: str, provenance: str,
) -> int:
    """Withdraw the open decisions on plans this thread owns. The caller owns the commit.

    A decision always lives in its author's own plan, so archiving a thread
    closes every open question it raised; none is left to answer afterwards.
    """
    rows = db.execute(
        _OWNED_BY_THREAD_SQL,
        (decision_projection.DECISION_TYPE, thread_id),
    ).fetchall()
    withdrawn = 0
    for row in rows:
        if _withdraw(
            db, row, reason=reason, actor_id=f"lifecycle:{thread_id}",
            provenance=provenance,
        ):
            withdrawn += 1
    return withdrawn


#: The question whose review node this is.
_ON_REVIEW_NODE_SQL = _ACTIVE_DECISIONS_SQL + """
       AND l.node_id = ?
"""
#: Why a question left the queue when the user chose Won't do on it.
USER_DROPPED = "The user chose Won't do"


def withdraw_for_review_node(db, node_id: str, *, actor_id: str, provenance: str) -> int:
    """Withdraw the open question this review node carries: the user's Won't do.

    The same total withdrawal a thread archive performs: attention cleared,
    review node closed, decision invalidated, plan re-admitted. The caller owns
    the commit.
    """
    rows = db.execute(
        _ON_REVIEW_NODE_SQL, (decision_projection.DECISION_TYPE, node_id),
    ).fetchall()
    return sum(
        _withdraw(db, row, reason=USER_DROPPED, actor_id=actor_id, provenance=provenance)
        for row in rows
    )


def withdraw_for_closed_source(
    db, node_id: str, new_state: str, *, actor_id: str | None, provenance: str,
) -> int:
    """Withdraw the open decisions drafted from a task that just closed.

    Runs inside the closing transition's own write; the caller owns the commit.
    The question's review node closes through the same transition, whose own
    hook finds nothing, because no decision is ever drafted from a review node.
    """
    if new_state != node_states.CLOSED:
        return 0
    return withdraw_for_source(
        db, node_id, reason=SOURCE_CLOSED, actor_id=actor_id, provenance=provenance,
    )


def withdraw_for_source(
    db, node_id: str, *, reason: str, actor_id: str | None, provenance: str,
) -> int:
    """Withdraw the open decisions drafted from this task: it no longer needs them.

    A closed task, or one the user marked done (``plans.user_settle``). The
    caller owns the commit.
    """
    plan = db.execute(
        "SELECT plan_id FROM plan_nodes WHERE id=?", (node_id,),
    ).fetchone()
    if plan is None:
        return 0
    rows = db.execute(
        _FROM_SOURCE_SQL,
        (decision_projection.DECISION_TYPE, plan["plan_id"], node_id),
    ).fetchall()
    withdrawn = 0
    for row in rows:
        if _withdraw(
            db, row, reason=reason,
            actor_id=actor_id or f"lifecycle:{node_id}",
            provenance=provenance, readmit=False,
        ):
            withdrawn += 1
    return withdrawn
