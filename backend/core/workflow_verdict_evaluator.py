"""Recording workflow and lesson verdicts inside the evaluator's own transaction.

The daily job (``jobs.workflow_verdicts``) reads open workflow versions with ``core.workflow_verdict_read``
(``read_open_snapshot``, then ``price_snapshot``) and open lesson trials with ``core.lesson_trial_verdicts``,
computes off the database, then calls :func:`write_verdicts` inside the ``write_transaction`` it holds. Nothing
here opens a transaction or reads to aggregate.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Iterable

from loguru import logger

from core.workflow_verdict_read import PLANNED_EVENT, REACHED_EVENT
from core.workflow_verdict_rules import Evaluation, Verdict
from core.workflow_verdict_types import split_subject
from infra.outcome_events import record_outcome_event
from infra.send_types import SendRejected
from tools.workflow_task_fields import version_short_hash

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
            if result.inserted and wake_owners and evaluation.owner is None:
                logger.warning("verdict_wake: no owner to wake for {}", plan.subject)
            if result.inserted and wake_owners and evaluation.owner is not None:
                from events.delivery import enqueue_event_wake

                kind, name, _ = split_subject(plan.subject)
                action = "keep the change" if verdict.word == "keep" else "revert the change"
                try:
                    enqueue_event_wake(
                        evaluation.owner, REACHED_EVENT,
                        f"{kind.capitalize()} {name} ({version_short_hash(plan.version)}) verdict reached: {action}.",
                        {"subject": plan.subject, "verdict": verdict.word, "reason": verdict.reason},
                        message_id=f"verdict:wake:{plan.subject}", db=connection,
                    )
                # An owner gone, being deleted, or whose model can't take input refuses only its own wake: the
                # savepoint rolls that back and the verdict stays recorded, so one owner never undoes the batch.
                except (ValueError, SendRejected) as refused:
                    logger.warning("verdict_wake: owner {} refused for {}: {}", evaluation.owner, plan.subject, refused)
    return recorded
