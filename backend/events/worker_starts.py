"""``start_worker`` rules: a matching event queues one worker start per request.

Evaluation runs inside the event's write transaction, and a start makes a thread,
so the rule's row in ``event_route_decisions`` is the start's durable queue, its
request identity (R24) and, once started, its worker's record. Evaluation records it
``queued`` with the start's input and the identity, or ``skipped`` (a duplicate
request, a loop, a missing identity); the worker runner claims it and makes it
``started`` (or ``held`` at a full worker limit, with its worker already made), or
ends it ``skipped`` or ``refused``; the worker's result settles it once as
``returned``, ``invalid`` or ``no_result``.

R24: while a request's start is queued, held, started, or has returned a result
(valid or not), another match of the same rule and identity starts nothing. A start
that ends with nothing returned, or is skipped or refused, releases the identity.
Identities age out with the row, after ``rules.RETENTION``. Names no connector.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from events.rule_outcomes import (
    DROPPED, HELD, INVALID, NO_RESULT, QUEUED, REFUSED, RETURNED, SKIPPED, START_WORKER, STARTED,
    Outcome, Rule, RuleEvent,
)

DUPLICATE_REQUEST = "duplicate request"
WORKER_CAPACITY = "worker capacity"  # why a made worker's first turn waits
LOOP = "loop"
#: The outcomes that keep a request's identity in use.
IN_USE = (QUEUED, HELD, STARTED, RETURNED, INVALID)
#: A start whose worker is made and whose result has not settled it.
LIVE = (HELD, STARTED)
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
    identity fact, or a request whose start is in use)."""
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
    """Whether any start is queued, held or started: the runner's one read when it has none."""
    from infra.database import get_db

    connection = db if db is not None else get_db()
    return connection.execute(
        "SELECT 1 FROM event_route_decisions WHERE outcome IN (?,?,?) "
        "AND request_identity IS NOT NULL LIMIT 1", (QUEUED, *LIVE),
    ).fetchone() is not None


def queued(db: sqlite3.Connection, limit: int) -> list[Mapping[str, Any]]:
    """Up to ``limit`` queued starts, oldest first, with their rule's workflow and whether
    it is still on. No start's input is read here."""
    if limit <= 0:
        return []
    return db.execute(
        "SELECT d.id, d.rule_id, d.thread_id, d.request_identity, d.event_id, d.connector, "
        "d.event_type, d.artifact_integration, d.artifact_type, d.artifact_external_id, "
        "r.workflow_id, r.enabled "
        "FROM event_route_decisions d LEFT JOIN event_rules r ON r.id=d.rule_id "
        "WHERE d.outcome=? AND d.request_identity IS NOT NULL ORDER BY d.id LIMIT ?", (QUEUED, limit),
    ).fetchall()


def claim(db: sqlite3.Connection, decision_id: int) -> dict[str, Any] | None:
    """Claim one queued start exactly once and return its input, which leaves storage with
    the claim; ``None`` if it was no longer queued. A claimed start is ``started`` with no
    worker until :func:`made` records one; :func:`orphans` finds one a restart cut short."""
    row = db.execute("SELECT run_input FROM event_route_decisions WHERE id=? AND outcome=?",
                     (decision_id, QUEUED)).fetchone()
    if row is None or db.execute(
        "UPDATE event_route_decisions SET outcome=?, run_input=NULL WHERE id=? AND outcome=?",
        (STARTED, decision_id, QUEUED),
    ).rowcount != 1:
        return None
    return json.loads(row["run_input"] or "{}")


def made(db: sqlite3.Connection, decision_id: int, worker: str, *, held: bool, check: str) -> None:
    """Record the claimed start's worker, whether its first turn waits for a slot, and
    ``check``, what its result is checked and reported with, kept until it settles."""
    db.execute(
        "UPDATE event_route_decisions SET worker_thread_id=?, outcome=?, reason=?, run_input=? "
        "WHERE id=? AND outcome=?",
        (worker, HELD if held else STARTED, WORKER_CAPACITY if held else None, check,
         decision_id, STARTED),
    )


def end(db: sqlite3.Connection, decision_id: int, outcome: str, reason: str | None = None) -> bool:
    """Settle one start once: skipped or refused before a worker, or the worker's
    ``returned``, ``invalid`` or ``no_result``. False if it was already settled."""
    assert outcome in (SKIPPED, REFUSED, RETURNED, INVALID, NO_RESULT), outcome
    marks = ",".join("?" * (1 + len(LIVE)))
    return db.execute(
        f"UPDATE event_route_decisions SET outcome=?, reason=?, run_input=NULL "
        f"WHERE id=? AND outcome IN ({marks})",
        (outcome, reason[:_REASON_LIMIT] if reason else None, decision_id, QUEUED, *LIVE),
    ).rowcount == 1


def started_by(db: sqlite3.Connection, worker: str) -> Mapping[str, Any] | None:
    """The live start whose worker is ``worker``, with its rule (``idx_..._worker``)."""
    marks = ",".join("?" * len(LIVE))
    return db.execute(
        "SELECT d.id, d.rule_id, d.thread_id, d.outcome, d.request_identity, d.event_id, "
        "d.artifact_integration, d.artifact_type, d.artifact_external_id, r.workflow_id "
        "FROM event_route_decisions d JOIN event_rules r ON r.id=d.rule_id "
        f"WHERE d.worker_thread_id=? AND d.outcome IN ({marks})", (worker, *LIVE),
    ).fetchone()


_START = ("SELECT id, rule_id, thread_id, worker_thread_id, request_identity, event_id, connector, "
          "event_type, artifact_integration, artifact_type, artifact_external_id, run_input "
          "FROM event_route_decisions ")


def with_workers(db: sqlite3.Connection, outcome: str) -> list[Mapping[str, Any]]:
    """Every held or started start with its worker made, oldest first, with what its
    result is checked and reported with."""
    assert outcome in LIVE, outcome
    return db.execute(_START + "WHERE outcome=? AND worker_thread_id IS NOT NULL ORDER BY id",
                      (outcome,)).fetchall()


def orphans(db: sqlite3.Connection) -> list[Mapping[str, Any]]:
    """Claimed starts with no worker. The runner claims and makes each under one lock, so
    when it holds that lock, one of these is a start a restart cut short."""
    return db.execute(
        _START + "WHERE outcome=? AND worker_thread_id IS NULL AND request_identity IS NOT NULL",
        (STARTED,),
    ).fetchall()


def held_before(db: sqlite3.Connection, worker: str) -> int:
    """Held starts older than ``worker``'s whose worker has no turn running: the ones a
    freed slot goes to first. Only a held worker's own start makes this count."""
    row = db.execute(
        "SELECT id FROM event_route_decisions WHERE worker_thread_id=? AND outcome=?",
        (worker, HELD),
    ).fetchone()
    if row is None:
        return 0
    return db.execute(
        "SELECT COUNT(*) FROM event_route_decisions d WHERE d.outcome=? AND d.id<? "
        "AND NOT EXISTS (SELECT 1 FROM active_turns t WHERE t.thread_id=d.worker_thread_id "
        "AND t.status='running')",
        (HELD, row["id"]),
    ).fetchone()[0]


def running(db: sqlite3.Connection, decision_id: int) -> None:
    """A held start whose worker's first turn has begun."""
    db.execute("UPDATE event_route_decisions SET outcome=?, reason=NULL WHERE id=? AND outcome=?",
               (STARTED, decision_id, HELD))
