"""The daily evaluator boundary for slice-1 workflow verdicts.

Reads and bootstrap work deliberately happen before the one short per-owner
transaction.  No scheduling, delivery, or wake is introduced in this slice.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Iterable, Mapping

from core.workflow_verdicts import FrozenPlan, Verdict, WorkflowRun, assert_evaluator_fields, decide, freeze_plan
from infra.database import write_transaction
from infra.db_async import run_in_db, run_off_db, run_read
from infra.outcome_events import record_outcome_event

# Calibration enables this only in a later slice.  Keeping it a code constant
# makes production evaluation read-only until an explicit reviewed change.
VERDICT_WRITES_ENABLED = False


@dataclass(frozen=True)
class Evaluation:
    owner: str | None
    plan: FrozenPlan
    verdict: Verdict
    plan_is_new: bool


def evaluate_snapshot(subject: str, version: str, baseline: str, runs: Iterable[WorkflowRun],
                      definitions: Iterable[Mapping[str, object]], owner: str | None,
                      replaced_by: str | None = None, existing_plan: FrozenPlan | None = None) -> Evaluation:
    """Apply the evaluator's deciding-field checks and the frozen rule."""
    plan = existing_plan or freeze_plan(subject, baseline, assert_evaluator_fields(definitions))
    return Evaluation(owner, plan, decide(tuple(runs), version, plan, replaced_by=replaced_by), existing_plan is None)


def _write_one(db: sqlite3.Connection, evaluation: Evaluation) -> bool:
    """One transaction for this owner's plan and final verdict records."""
    if not VERDICT_WRITES_ENABLED:
        return False
    plan, verdict = evaluation.plan, evaluation.verdict
    now = datetime.now(timezone.utc).isoformat()
    detail = json.dumps({"reason": verdict.reason, "look": verdict.look,
                         "calls": [asdict(item) for item in verdict.calls]}, sort_keys=True, separators=(",", ":"))
    with write_transaction(db, scope="measurement_verdict_evaluator") as connection:
        if evaluation.plan_is_new:
            record_outcome_event(connection, dedupe_key=f"verdict:planned:{plan.subject}",
                                 event_name="measurement.verdict.planned", source="corsia", provenance="instrumented",
                                 ts=now, subject_key=plan.subject, agent_thread_id=evaluation.owner,
                                 attributes={"plan": plan.encoded()})
        if verdict.word not in {"collecting", "not_enough_data"}:
            result = record_outcome_event(connection, dedupe_key=f"verdict:reached:{plan.subject}",
                                          event_name="measurement.verdict.reached", source="corsia",
                                          provenance="instrumented", ts=now, subject_key=plan.subject,
                                          agent_thread_id=evaluation.owner,
                                          attributes={"verdict": verdict.word, "reason": verdict.reason,
                                                      "detail": detail})
            return result.inserted
    return False


async def evaluate_daily(read_snapshot: Callable[[], object], compute: Callable[[object], Evaluation],
                         db_factory: Callable[[], sqlite3.Connection]) -> bool:
    """Run the required read → off-DB compute → short write sequence.

    The caller owns snapshot shape and invokes this once per owner.  The
    connection is opened inside the DB lane, so it never crosses thread
    ownership boundaries. Database aggregation is never performed after
    ``BEGIN IMMEDIATE``.
    """
    snapshot = await run_read(read_snapshot)
    evaluation = await run_off_db(compute, snapshot)

    def write() -> bool:
        db = db_factory()
        try:
            return _write_one(db, evaluation)
        finally:
            db.close()

    return await run_in_db(write)
