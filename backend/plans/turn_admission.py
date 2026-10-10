"""Thread-owned plan reconciliation performed before a Turn takes its snapshot."""

from __future__ import annotations

from dataclasses import dataclass

import plans.selection_validity as validity
import plans.service as service


def owner_active_plan_id(db, thread_id: str) -> str | None:
    """Return this thread's active plan; paused plans select nothing."""
    row = db.execute(
        "SELECT id FROM plans WHERE owner_kind='thread' AND owner_id=? AND state='active'",
        (thread_id,),
    ).fetchone()
    return str(row["id"]) if row is not None else None


def _open_attempt_reconciliation_needed(db, plan_id: str) -> bool | None:
    """Return whether open attempts need work, or None when there are none."""
    open_rows = db.execute(
        "SELECT * FROM plan_node_attempts WHERE plan_id=? AND released_at IS NULL",
        (plan_id,),
    ).fetchall()
    if not open_rows:
        return None
    valid_ids = {str(row["id"]) for row in validity.valid_selections(db, open_rows)}
    return any(str(row["id"]) not in valid_ids for row in open_rows)


@dataclass(frozen=True, slots=True)
class _ReconciliationDecision:
    """Whether to reconcile and any first eligible task already read to decide it."""

    could_help: bool
    candidate_ids: tuple[str, ...] | None = None


def _owner_reconciliation_decision(db, plan_id: str) -> _ReconciliationDecision:
    """Decide once whether a single-executor thread plan can change."""
    plan = service._plan_row(db, plan_id)
    if plan["owner_kind"] != "thread":
        raise service.PlanError("owner reconciliation requires a thread-owned plan")
    attempts_need_work = _open_attempt_reconciliation_needed(db, plan_id)
    if attempts_need_work is not None:
        return _ReconciliationDecision(attempts_need_work)
    eligible_id = validity.first_eligible_active_id(db, plan)
    candidate_ids = (eligible_id,) if eligible_id is not None else ()
    return _ReconciliationDecision(bool(candidate_ids), candidate_ids)


def snapshot_after_admission(
    thread_id: str,
    *,
    actor_id: str | None = None,
    provenance: str = "turn_admission",
    db,
) -> dict | None:
    """Reconcile this thread's own plan, then return its Turn-start snapshot."""
    from plans.admission import reconcile_plan
    from plans.admission_origin import turn_snapshot

    plan_id = owner_active_plan_id(db, thread_id)
    if plan_id is not None:
        decision = _owner_reconciliation_decision(db, plan_id)
        if decision.could_help:
            with service._write(db):
                reconcile_plan(
                    plan_id,
                    actor_id=actor_id,
                    provenance=provenance,
                    causing_executor=thread_id,
                    candidate_ids=decision.candidate_ids,
                    db=db,
                )
                return turn_snapshot(thread_id, db=db)
    return turn_snapshot(thread_id, db=db)
