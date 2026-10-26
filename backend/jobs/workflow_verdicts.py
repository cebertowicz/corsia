"""Bounded model-free daily evaluation of open workflow verdicts.

The nightly scheduler owns the once-per-day cadence. This job reads one shared
batch of planned-without-reached workflow versions, evaluates it in memory, and
writes/wakes only when the existing verdict switch is explicitly enabled.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from loguru import logger

from core import workflow_verdict_evaluator as evaluator
from core.workflow_verdict_read import OPEN_BATCH_LIMIT, price_snapshot, read_open_snapshot
from core.workflow_verdict_rules import compute
from infra.database import get_db, write_transaction


@dataclass(frozen=True)
class DailyVerdictReport:
    open_subjects: int
    evaluated: int
    reached: int
    recorded: int


def run_daily(*, now: datetime | None = None, batch_limit: int = OPEN_BATCH_LIMIT) -> DailyVerdictReport:
    """Evaluate one bounded open batch; read-only while verdict writes remain disabled."""
    current = now or datetime.now(timezone.utc)
    db = get_db()
    try:
        snapshot = read_open_snapshot(db, now=current, limit=batch_limit)
        priced = price_snapshot(db, snapshot)
    finally:
        db.close()
    evaluations = compute(priced)
    reached = sum(item.verdict.word in {"keep", "revert"} for item in evaluations)
    recorded = 0
    if evaluator.VERDICT_WRITES_ENABLED:
        with write_transaction(None, scope="workflow_verdict_daily") as db:
            recorded = evaluator.write_verdicts(db, evaluations, wake_owners=True)
    report = DailyVerdictReport(len(snapshot.subjects or ()), len(evaluations), reached, recorded)
    logger.info("workflow_verdict_daily: open={} evaluated={} reached={} recorded={}",
                report.open_subjects, report.evaluated, report.reached, report.recorded)
    return report
