"""Bounded model-free daily evaluation of open workflow versions and lesson trials.

The nightly scheduler owns the once-per-day cadence. This job reads one shared batch of open workflow
versions (in use, no reached verdict) and one of open lesson trials (delivered, no reached verdict), evaluates both in
memory, and writes and wakes the owner only when the existing verdict switch is explicitly enabled.
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from loguru import logger

from core import workflow_verdict_evaluator as evaluator
from core import workflow_verdict_detail as detail
from core.lesson_trial_verdicts import TrialRead, evaluate_trials
from core.workflow_verdict_read import OPEN_BATCH_LIMIT, price_snapshot, read_open_snapshot
from core.workflow_verdict_rules import Snapshot, compute
from infra.database import get_db, write_transaction


@dataclass(frozen=True)
class DailyVerdictReport:
    open_subjects: int
    evaluated: int
    reached: int
    recorded: int
    by_word: dict[str, int] = field(default_factory=dict)
    by_reason: dict[str, int] = field(default_factory=dict)
    workflow_runs: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    woken: int = 0
    detail: dict[str, object] = field(default_factory=dict)


def run_counts(snapshot: Snapshot) -> dict[str, int]:
    """How much of the workflow batch's settled history has a cost, by run and by version, all runs and outcomes only.

    A version with no outcome has nothing to price, so it is not counted as outcomes priced.
    """
    by_version: defaultdict[tuple[str, str], list] = defaultdict(list)
    for history in snapshot.workflows:
        for run in history.runs:
            if run.terminal_at:
                by_version[(history.ref, run.version)].append(run)
    runs = [run for version in by_version.values() for run in version]
    outcomes = [run for run in runs if run.outcome]
    return {"settled": len(runs), "priced": sum(run.cost is not None for run in runs),
            "outcomes": len(outcomes), "outcomes_priced": sum(run.cost is not None for run in outcomes),
            "versions": len(by_version),
            "versions_priced": sum(all(run.cost is not None for run in version) for version in by_version.values()),
            "versions_outcomes_priced": sum(
                any(run.outcome for run in version) and all(run.cost is not None for run in version if run.outcome)
                for version in by_version.values())}


def _detail(db, snapshot: Snapshot, read: TrialRead, evaluations: list, lessons: list,
            phases: dict[str, float]) -> dict[str, object]:
    """The log line's extra aggregates; they read only and change no verdict."""
    started = time.monotonic()
    keys = [(item.lesson_id, item.sha) for item in read.trials]
    run_ids = [run.run_id for history in snapshot.workflows for run in history.runs if run.terminal_at]
    found: dict[str, object] = {
        "by_kind": detail.by_kind({"workflow": len(snapshot.subjects or ()), "lesson": len(lessons)}, evaluations),
        "reached_detail": detail.reached_detail(evaluations),
        **detail.lesson_halves((len(arms.with_runs), len(arms.without_runs)) for arms in read.arms.values()),
        "sent_back": detail.sent_back(db, keys, run_ids),
        "delegated_receipts": detail.delegated_receipts(
            db, keys, min((item.first_at for item in read.trials), default=None))}
    phases["detail"] = time.monotonic() - started
    return found


def run_daily(*, now: datetime | None = None, batch_limit: int = OPEN_BATCH_LIMIT) -> DailyVerdictReport:
    """Evaluate one bounded open batch of each kind; read-only while verdict writes remain disabled."""
    started = time.monotonic()
    current = now or datetime.now(timezone.utc)
    db = get_db()
    phases: dict[str, float] = {}
    read = TrialRead()
    try:
        lap = time.monotonic()
        snapshot = read_open_snapshot(db, now=current, limit=batch_limit)
        phases["workflow_read"], lap = time.monotonic() - lap, time.monotonic()
        priced = price_snapshot(db, snapshot)
        phases["workflow_pricing"] = time.monotonic() - lap
        lessons = evaluate_trials(db, current, limit=batch_limit, read=read)
        lap = time.monotonic()
        evaluations = [*compute(priced), *lessons]
        phases.update(read.seconds)
        phases["decide"] = phases.pop("lesson_decide", 0.0) + time.monotonic() - lap
        extra = _detail(db, priced, read, evaluations, lessons, phases)
    finally:
        db.close()
    by_word = Counter(item.verdict.word for item in evaluations)
    reached = sum(count for word, count in by_word.items() if word in evaluator.STORED)
    recorded = woken = 0
    if evaluator.VERDICT_WRITES_ENABLED:
        with write_transaction(None, scope="workflow_verdict_daily") as db:
            recorded = evaluator.write_verdicts(db, evaluations)
            woken = evaluator.wake_owners(db)
    report = DailyVerdictReport(len(snapshot.subjects or ()) + len(lessons), len(evaluations), reached, recorded,
                                dict(by_word), dict(Counter(item.verdict.reason for item in evaluations)),
                                run_counts(priced), round(time.monotonic() - started, 1), woken,
                                {**extra, "phase_seconds": {name: round(value, 1) for name, value in phases.items()}})
    logger.info("workflow_verdict_daily: open={} evaluated={} reached={} recorded={} woken={} by_word={} by_reason={} "
                "workflow_runs={} seconds={} phase_seconds={} by_kind={} reached_detail={} lesson_halves={} lesson_both_20={} "
                "sent_back={} delegated_receipts={}", report.open_subjects, report.evaluated, report.reached,
                report.recorded, report.woken, report.by_word, report.by_reason, report.workflow_runs, report.seconds,
                *(report.detail.get(name) for name in (
                    "phase_seconds", "by_kind", "reached_detail", "lesson_halves", "lesson_both_20", "sent_back",
                    "delegated_receipts")))
    return report
