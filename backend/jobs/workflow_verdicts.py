"""Bounded model-free daily evaluation of open workflow versions and lesson trials.

The nightly scheduler owns the once-per-day cadence. This job reads one shared batch of open workflow
versions (in use, no reached verdict) and one of open lesson trials (delivered, no reached verdict), evaluates both in
memory, and writes and wakes the owner only when the existing verdict switch is explicitly enabled.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from loguru import logger

from core import workflow_verdict_evaluator as evaluator
from core.lesson_trial_verdicts import evaluate_trials
from core.workflow_verdict_read import OPEN_BATCH_LIMIT, price_snapshot, read_open_snapshot
from core.workflow_verdict_rules import compute
from infra.database import get_db, write_transaction

REACHED_WORDS = frozenset({"keep", "revert"})


@dataclass(frozen=True)
class DailyVerdictReport:
    open_subjects: int
    evaluated: int
    reached: int
    recorded: int
    by_word: dict[str, int] = field(default_factory=dict)


def run_daily(*, now: datetime | None = None, batch_limit: int = OPEN_BATCH_LIMIT) -> DailyVerdictReport:
    """Evaluate one bounded open batch of each kind; read-only while verdict writes remain disabled."""
    current = now or datetime.now(timezone.utc)
    db = get_db()
    try:
        snapshot = read_open_snapshot(db, now=current, limit=batch_limit)
        priced = price_snapshot(db, snapshot)
        lessons = evaluate_trials(db, current, limit=batch_limit)
    finally:
        db.close()
    evaluations = [*compute(priced), *lessons]
    by_word = Counter(item.verdict.word for item in evaluations)
    reached = sum(count for word, count in by_word.items() if word in REACHED_WORDS)
    recorded = 0
    if evaluator.VERDICT_WRITES_ENABLED:
        with write_transaction(None, scope="workflow_verdict_daily") as db:
            recorded = evaluator.write_verdicts(db, evaluations, wake_owners=True)
    report = DailyVerdictReport(len(snapshot.subjects or ()) + len(lessons), len(evaluations), reached, recorded,
                                dict(by_word))
    logger.info("workflow_verdict_daily: open={} evaluated={} reached={} recorded={} by_word={}",
                report.open_subjects, report.evaluated, report.reached, report.recorded, report.by_word)
    return report
