"""Recording slice-1 workflow verdicts inside the evaluator's own transaction.

The evaluator job is KPI slice 4's (KPI design §4.7); this slice adds no job, schedule or wake. That job reads
with ``core.workflow_verdict_read`` (``read_snapshot`` on ``run_read``, ``price_snapshot`` on ``run_bulk``),
computes with ``core.workflow_verdict_rules.compute`` off the database, then calls :func:`write_verdicts` inside
the ``write_transaction`` it already holds. Nothing here opens a transaction or reads to aggregate.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Iterable

from core.workflow_verdict_read import PLANNED_EVENT, REACHED_EVENT
from core.workflow_verdict_rules import Evaluation, Verdict
from infra.outcome_events import record_outcome_event

# Calibration enables this only in a later slice. A code constant keeps production evaluation
# read-only until an explicit reviewed change.
VERDICT_WRITES_ENABLED = False
# Changed again, Collecting and Not enough data are computed on read: a replaced version can return to use.
STORED = frozenset({"keep", "revert"})
DETAIL_LIMIT = 2_000  # infra.outcome_event_attributes: measurement.verdict.reached detail


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def detail_text(verdict: Verdict) -> str:
    """The reached record's detail: look, template inputs and each call, compact; inputs drop first if long."""
    calls = [[c.name, c.call, c.reason, _round(c.difference), *(map(_round, c.interval or (None, None))),
              _round(c.margin), c.n_new, c.n_base] for c in verdict.calls]
    for data in ({"look": verdict.look, "inputs": verdict.inputs, "calls": calls},
                 {"look": verdict.look, "calls": calls}, {"look": verdict.look}):
        text = json.dumps(data, sort_keys=True, separators=(",", ":"), default=list)
        if len(text) <= DETAIL_LIMIT:
            return text
    raise AssertionError("unreachable: a look alone fits")


def write_verdicts(connection: sqlite3.Connection, evaluations: Iterable[Evaluation], *, wake_owners: bool = False) -> int:
    """Record new frozen plans and reached verdicts; optional wakes are only for newly reached results.

    Idempotent through each record's dedupe key, so a reached verdict is never replaced (VR-3).
    """
    if not VERDICT_WRITES_ENABLED:
        return 0
    now, recorded = datetime.now(timezone.utc).isoformat(), 0
    for evaluation in evaluations:
        plan, verdict = evaluation.plan, evaluation.verdict
        if evaluation.plan_is_new:
            record_outcome_event(connection, dedupe_key=f"verdict:planned:{plan.subject}", event_name=PLANNED_EVENT,
                                 source="corsia", provenance="instrumented", ts=now, subject_key=plan.subject,
                                 agent_thread_id=evaluation.owner, attributes={"plan": plan.encoded()})
        if verdict.word in STORED:
            result = record_outcome_event(connection, dedupe_key=f"verdict:reached:{plan.subject}",
                                          event_name=REACHED_EVENT, source="corsia", provenance="instrumented",
                                          ts=now, subject_key=plan.subject, agent_thread_id=evaluation.owner,
                                          attributes={"verdict": verdict.word, "reason": verdict.reason,
                                                      "detail": detail_text(verdict)})
            recorded += int(result.inserted)
            if result.inserted and wake_owners and evaluation.owner is not None:
                from events.delivery import enqueue_event_wake

                enqueue_event_wake(
                    evaluation.owner, REACHED_EVENT, f"Workflow verdict reached: {verdict.word}.",
                    {"subject": plan.subject, "verdict": verdict.word, "reason": verdict.reason},
                    message_id=f"verdict:wake:{plan.subject}", db=connection,
                )
    return recorded
