"""Where one Turn's work is attributed: its plan node, and its accounting snapshot.

Split from ``plans.admission`` because it answers a different question. Admission
decides which task may hold an executor's slot; this module reads the selection
that decision left behind and tells an artifact registration and a starting Turn
which node their cost and provenance belong to. It writes nothing; Turn start
calls it only after ``turn_admission.snapshot_after_admission`` has reconciled
work.
"""

from __future__ import annotations

from dataclasses import dataclass

import plans.selection_validity as validity
import plans.service as service


@dataclass(frozen=True)
class OriginResolution:
    """The plan node one artifact registration binds to, and why."""

    outcome: str
    reason: str
    node_id: str | None = None
    node_state: str | None = None
    usage_role: str | None = None


def registration_origin(thread_id: str, turn_id: str, *, db) -> OriginResolution:
    """Resolve the plan node for an exact server-issued Turn identity.

    Turn identity is checked first (`turn_unknown`, `turn_thread_mismatch`,
    `turn_not_live`). The thread's canonical attributed valid selection then
    names the node; only without one does the Turn's write-once accounting
    snapshot decide, under the archived and terminal rules (a reviewer's
    verification Turn selects nothing of its own).
    """
    turn = db.execute(
        """SELECT thread_id,status,plan_node_id,plan_usage_role
           FROM active_turns WHERE id=?""",
        (turn_id,),
    ).fetchone()
    if turn is None:
        return OriginResolution("unbound", "turn_unknown")
    if turn["thread_id"] != thread_id:
        return OriginResolution("unbound", "turn_thread_mismatch")
    if turn["status"] != "running":
        return OriginResolution("unbound", "turn_not_live")
    selected = validity.attributed_selection(db, thread_id)
    if selected is None:
        return _turn_snapshot_origin(db, turn)
    from plans import commitments

    node_id = str(selected["node_id"])
    return OriginResolution(
        "bound", "bound", node_id, "active", commitments.usage_role(node_id, db=db),
    )


def _turn_snapshot_origin(db, turn) -> OriginResolution:
    """Resolve origin from a live Turn's accounting snapshot."""
    if turn["plan_node_id"] is None:
        return OriginResolution("unbound", "turn_unbound")
    node = db.execute(
        "SELECT state,archived_at FROM plan_nodes WHERE id=?",
        (turn["plan_node_id"],),
    ).fetchone()
    if node is None:
        return OriginResolution("unbound", "node_missing")
    if node["archived_at"] is not None:
        return OriginResolution("unbound", "node_archived")
    if node["state"] in service._TERMINAL_STATES:
        return OriginResolution("unbound", "node_terminal")
    return OriginResolution(
        "bound", "bound", str(turn["plan_node_id"]), str(node["state"]),
        turn["plan_usage_role"],
    )


def turn_snapshot(executor: str, *, db) -> dict | None:
    """Return the accounting snapshot a starting Turn records, or None."""
    attempt = validity.attributed_selection(db, executor)
    if attempt is None:
        return None
    from plans import commitments

    return {
        "plan_id": attempt["plan_id"],
        "plan_node_id": attempt["node_id"],
        "plan_attempt_id": attempt["id"],
        "plan_commitment_id": attempt["commitment_id"],
        "plan_usage_role": commitments.usage_role(attempt["node_id"], db=db),
    }
