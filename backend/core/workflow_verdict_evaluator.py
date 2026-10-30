"""Recording workflow and lesson verdicts inside the evaluator's own transaction.

The daily job (``jobs.workflow_verdicts``) reads open workflow versions with ``core.workflow_verdict_read``
(``read_open_snapshot``, then ``price_snapshot``) and open lesson trials with ``core.lesson_trial_verdicts``,
computes off the database, then calls :func:`write_verdicts` and :func:`wake_owners` inside the
``write_transaction`` it holds. Nothing here opens a transaction or reads to aggregate.
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
from infra.queries import thread_available_sql
from infra.send_types import SendRejected
from tools.workflow_task_fields import version_short_hash

# Calibration enables this only in a later slice. A code constant keeps production evaluation
# read-only until an explicit reviewed change.
VERDICT_WRITES_ENABLED = False
# Changed again, Collecting and Not enough data are computed on read: a replaced version can return to use.
STORED = frozenset({"keep", "revert"})
DETAIL_LIMIT = 2_000  # infra.outcome_event_attributes: measurement.verdict.reached detail
# A reached verdict whose owner can still be woken and whose wake message does not exist yet. The verdict is
# recorded once; its wake is tried every night until accepted, so a refusal the person can fix (a model whose
# cap can't fit a checkpoint) reaches the owner after the fix. A purged or user-archived owner is never tried.
_UNWOKEN_SQL = (
    "SELECT r.subject_key, r.agent_thread_id, r.attributes FROM outcome_events r "
    "JOIN threads t ON t.id=r.agent_thread_id "
    f"WHERE r.event_name=? AND r.retracted_at IS NULL AND {thread_available_sql('t')} "
    "AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.id='verdict:wake:'||r.subject_key) "
    "ORDER BY r.ts, r.subject_key"
)


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


def write_verdicts(connection: sqlite3.Connection, evaluations: Iterable[Evaluation]) -> int:
    """Record new frozen plans and reached verdicts; :func:`wake_owners` tells their owners.

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
            if result.inserted and evaluation.owner is None:
                logger.warning("verdict_wake: no owner to wake for {}", plan.subject)
    return recorded


def wake_owners(connection: sqlite3.Connection) -> int:
    """Wake each reached verdict's owner whose wake was never accepted; returns how many were accepted now."""
    from events.delivery import enqueue_event_wake

    woken = 0
    for subject, owner, attributes in connection.execute(_UNWOKEN_SQL, (REACHED_EVENT,)).fetchall():
        recorded = json.loads(attributes)
        kind, name, version = split_subject(subject)
        action = "keep the change" if recorded["verdict"] == "keep" else "revert the change"
        try:
            enqueue_event_wake(
                owner, REACHED_EVENT,
                f"{kind.capitalize()} {name} ({version_short_hash(version)}) verdict reached: {action}.",
                {"subject": subject, "verdict": recorded["verdict"], "reason": recorded["reason"]},
                message_id=f"verdict:wake:{subject}", db=connection,
            )
            woken += 1
        # A refusal rolls back only this wake's savepoint: every verdict stays recorded and the next run tries again.
        except (ValueError, SendRejected) as refused:
            logger.warning("verdict_wake: owner {} refused for {}: {}", owner, subject, refused)
    return woken
