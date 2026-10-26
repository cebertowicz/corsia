"""``start_worker`` rules: a matching event queues one worker start per request.

Evaluation runs inside the event's write transaction, and a start makes a thread,
so the rule's row in ``event_route_decisions`` is the start's durable queue, its
request identity (R24) and, once started, its worker's record. Evaluation records it
``queued`` with the start's input and the identity, or ``skipped`` (a duplicate
request, a loop, a missing identity, or ``queue full`` once its rule already has
``rule_runs.QUEUE_CAP`` starts waiting, as a ``run_script`` rule's runs). It stays ``queued`` while the Claude Code limit
is full (R23), or, held with its reason, while its Local only owner has no local model
ready: no worker exists until the runner claims it with a free slot and makes
it ``started``, or ends it ``skipped`` or ``refused``. The worker's result settles it
once as ``returned``, ``invalid`` or ``no_result``.

R24: while a request's start is queued, started, or has returned a result (valid or
not), another match of the same rule and identity starts nothing. A start that ends
with nothing returned, or is skipped or refused, releases the identity. Identities age
out with the row, after ``rules.RETENTION``. Names no connector.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from events import rule_runs
from events.rule_outcomes import (
    DROPPED, INVALID, NO_RESULT, QUEUED, REFUSED, RETURNED, SKIPPED, START_WORKER, STARTED,
    Outcome, Rule, RuleEvent,
)

DUPLICATE_REQUEST = "duplicate request"
LOOP = "loop"
#: The outcomes that keep a request's identity in use.
IN_USE = (QUEUED, STARTED, RETURNED, INVALID)
_REASON_LIMIT = 500


def _in_use(db: sqlite3.Connection, rule_id: str, identity: str) -> bool:
    marks = ",".join("?" * len(IN_USE))
    return db.execute(
        "SELECT 1 FROM event_route_decisions WHERE rule_id=? AND request_identity=? "
        f"AND outcome IN ({marks}) LIMIT 1",
        (rule_id, identity, *IN_USE),
    ).fetchone() is not None


def queue(
    db: sqlite3.Connection, event: RuleEvent, matched: Iterable[Rule],
    dropped_by: Mapping[str, str],
) -> list[Outcome]:
    """Each matching ``start_worker`` rule's outcome: queued with its input and request
    identity, dropped by its writer's own drop, or skipped (in the event's chain, no
    identity fact, a request whose start is in use, or a full queue)."""
    outcomes = []
    for rule in (rule for rule in matched if rule.action == START_WORKER):
        writer = rule.created_by
        value = event.facts.get(rule.request_identity_fact or "")
        identity = None if value is None or value == "" else str(value)
        if writer in dropped_by:
            outcomes.append(Outcome(rule.id, writer, DROPPED,
                                    reason=f"dropped by rule {dropped_by[writer]}"))
        elif rule.id in event.trace:
            outcomes.append(Outcome(rule.id, writer, SKIPPED, reason=LOOP))
        elif identity is None:
            outcomes.append(Outcome(rule.id, writer, SKIPPED,
                                    reason=f"the event has no {rule.request_identity_fact}"))
        elif _in_use(db, rule.id, identity):
            outcomes.append(Outcome(rule.id, writer, SKIPPED, reason=DUPLICATE_REQUEST))
        elif rule_runs.waiting(db, rule.id, writer) >= rule_runs.QUEUE_CAP:
            outcomes.append(Outcome(rule.id, writer, SKIPPED, reason=rule_runs.QUEUE_FULL))
        else:
            outcomes.append(Outcome(rule.id, writer, QUEUED, request_identity=identity,
                                    run_input=json.dumps({
                "event": {"type": f"{event.connector}.{event.event_type}", "id": event.event_id,
                          "rule_id": rule.id, "facts": dict(event.facts),
                          "artifact": dict(event.artifact_ref) if event.artifact_ref else None},
                "trace": [*event.trace, rule.id],
            }, ensure_ascii=False, default=str)))
    return outcomes


def any_open(db: sqlite3.Connection | None = None) -> bool:
    """Whether any start is queued or started: the runner's one read when it has none."""
    from infra.database import get_db

    connection = db if db is not None else get_db()
    return connection.execute(
        "SELECT 1 FROM event_route_decisions WHERE outcome IN (?,?) "
        "AND request_identity IS NOT NULL LIMIT 1", (QUEUED, STARTED),
    ).fetchone() is not None


def queued(db: sqlite3.Connection, limit: int, after: int = 0) -> list[Mapping[str, Any]]:
    """Up to ``limit`` queued starts newer than ``after``, oldest first, with their rule's
    workflow and whether it is still on. No start's input is read here."""
    if limit <= 0:
        return []
    return db.execute(
        "SELECT d.id, d.rule_id, d.thread_id, d.request_identity, d.event_id, d.connector, "
        "d.event_type, d.artifact_integration, d.artifact_type, d.artifact_external_id, "
        "r.workflow_id, r.enabled "
        "FROM event_route_decisions d LEFT JOIN event_rules r ON r.id=d.rule_id "
        "WHERE d.outcome=? AND d.request_identity IS NOT NULL AND d.id>? ORDER BY d.id LIMIT ?",
        (QUEUED, after, limit),
    ).fetchall()


def claim(db: sqlite3.Connection, decision_id: int) -> dict[str, Any] | None:
    """Claim one queued start exactly once and return its input; ``None`` if it was no
    longer queued. A claimed start is ``started`` with no worker, its input kept, until
    :func:`made` records one or :func:`requeue` puts it back."""
    row = db.execute("SELECT run_input FROM event_route_decisions WHERE id=? AND outcome=?",
                     (decision_id, QUEUED)).fetchone()
    if row is None or db.execute(
        "UPDATE event_route_decisions SET outcome=?, reason=NULL WHERE id=? AND outcome=?",
        (STARTED, decision_id, QUEUED),
    ).rowcount != 1:
        return None
    return json.loads(row["run_input"] or "{}")


def requeue(db: sqlite3.Connection, decision_id: int) -> bool:
    """Put a claimed start with no worker back in the queue, at its place (its id)."""
    return db.execute(
        "UPDATE event_route_decisions SET outcome=? WHERE id=? AND outcome=? "
        "AND worker_thread_id IS NULL", (QUEUED, decision_id, STARTED),
    ).rowcount == 1


def hold(db: sqlite3.Connection, decision_id: int, reason: str) -> None:
    """Say why a queued start waits (``explain`` shows it); claiming it clears this."""
    db.execute("UPDATE event_route_decisions SET reason=? WHERE id=? AND outcome=? "
               "AND reason IS NOT ?", (reason, decision_id, QUEUED, reason))


def made(db: sqlite3.Connection, decision_id: int, worker: str, check: str) -> None:
    """Record the claimed start's worker and ``check``, what its result is checked and
    reported with, kept in place of its input until it settles."""
    db.execute(
        "UPDATE event_route_decisions SET worker_thread_id=?, run_input=? "
        "WHERE id=? AND outcome=? AND worker_thread_id IS NULL",
        (worker, check, decision_id, STARTED),
    )


def end(db: sqlite3.Connection, decision_id: int, outcome: str, reason: str | None = None) -> bool:
    """Settle one start once: skipped or refused before a worker, or the worker's
    ``returned``, ``invalid`` or ``no_result``. False if it was already settled."""
    assert outcome in (SKIPPED, REFUSED, RETURNED, INVALID, NO_RESULT), outcome
    return db.execute(
        "UPDATE event_route_decisions SET outcome=?, reason=?, run_input=NULL "
        "WHERE id=? AND outcome IN (?,?)",
        (outcome, reason[:_REASON_LIMIT] if reason else None, decision_id, QUEUED, STARTED),
    ).rowcount == 1


def started_by(db: sqlite3.Connection, worker: str) -> Mapping[str, Any] | None:
    """The started start whose worker is ``worker``, with its rule (``idx_..._worker``)."""
    return db.execute(
        "SELECT d.id, d.rule_id, d.thread_id, d.outcome, d.request_identity, d.event_id, "
        "d.artifact_integration, d.artifact_type, d.artifact_external_id, r.workflow_id "
        "FROM event_route_decisions d JOIN event_rules r ON r.id=d.rule_id "
        "WHERE d.worker_thread_id=? AND d.outcome=?", (worker, STARTED),
    ).fetchone()


_START = ("SELECT d.id, d.rule_id, d.thread_id, d.worker_thread_id, d.request_identity, d.event_id, "
          "d.connector, d.event_type, d.artifact_integration, d.artifact_type, d.artifact_external_id, "
          "d.run_input, r.workflow_id FROM event_route_decisions d "
          "LEFT JOIN event_rules r ON r.id=d.rule_id ")


def with_workers(db: sqlite3.Connection) -> list[Mapping[str, Any]]:
    """Every started start with its worker made, oldest first, with what its result is
    checked and reported with."""
    return db.execute(_START + "WHERE d.outcome=? AND d.worker_thread_id IS NOT NULL ORDER BY d.id",
                      (STARTED,)).fetchall()


def orphans(db: sqlite3.Connection) -> list[Mapping[str, Any]]:
    """Started starts a restart may have cut short: no worker, or a worker with no turn and
    no queued input. The runner claims and spawns under one lock, so when it holds that
    lock, one of these whose worker has no live process was cut short."""
    return db.execute(
        _START + "WHERE d.outcome=? AND d.request_identity IS NOT NULL AND (d.worker_thread_id IS NULL "
        "OR (NOT EXISTS (SELECT 1 FROM active_turns t WHERE t.thread_id=d.worker_thread_id) "
        "AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.thread_id=d.worker_thread_id "
        "AND m.input_order IS NOT NULL AND m.delivery_state='queued')))",
        (STARTED,),
    ).fetchall()
