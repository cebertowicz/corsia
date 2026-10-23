"""Immutable correction facts observed from plan review rejections.

The rejection event retains the feedback.  This projection keeps only its stable
identifier, so Measurement can count sent-backs without duplicating review text
or sending a correction note back to the worker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from core.corrections_recording import (
    CORRECTION_EVENT,
    LABEL_CORRECTION,
    SENT_BACK_SUBJECT_PREFIX,
    SOURCE_KIND_SENT_BACK,
    subject_key,
)

#: How far behind the newest recorded fact the nightly recovery rescans, and how many
#: rejections one run records; both bound the scan of ``plan_node_events``.
RECONCILE_WINDOW = timedelta(days=7)
RECONCILE_BATCH = 500

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SentBackCorrection:
    """The already-read data needed for one short correction-fact write."""

    rejection_event_id: str
    created_at: str
    project_id: str | None
    thread_id: str


def _persistent_owner(thread_id: str, db) -> str | None:
    """Resolve a temporary executor to its nearest persistent delegation owner."""
    from infra.queries import thread_is_temporary

    current: str | None = thread_id
    seen: set[str] = set()
    while current is not None and current not in seen:
        seen.add(current)
        row = db.execute(
            "SELECT id,spawned_by,agent_enabled FROM threads WHERE id=?", (current,),
        ).fetchone()
        if row is None:
            return None
        if not thread_is_temporary(row):
            return str(row["id"])
        current = row["spawned_by"]
    return None


def prepare_sent_back_correction(rejection_event_id: str, *, db) -> SentBackCorrection | None:
    """Read the rejection and its durable owner before opening the fact write."""
    row = db.execute(
        """SELECT e.id,e.created_at,n.*,p.owner_kind AS plan_owner_kind,p.owner_id AS plan_owner_id
           FROM plan_node_events e
           JOIN plan_nodes n ON n.id=e.node_id
           JOIN plans p ON p.id=n.plan_id
           WHERE e.id=? AND e.event='review_rejected'""",
        (rejection_event_id,),
    ).fetchone()
    if row is None:
        log.warning("sent-back correction skipped for rejection event %s: no such rejection", rejection_event_id)
        return None
    from plans.public import executor_of

    executor = executor_of(row, {"owner_kind": row["plan_owner_kind"], "owner_id": row["plan_owner_id"]})
    if executor is None:
        log.warning("sent-back correction skipped for rejection event %s: node has no executor", rejection_event_id)
        return None
    owner = _persistent_owner(executor, db)
    if owner is None:
        log.warning(
            "sent-back correction skipped for rejection event %s: executor %s has no persistent owner",
            rejection_event_id, executor,
        )
        return None
    project = db.execute("SELECT project_id FROM threads WHERE id=?", (owner,)).fetchone()
    return SentBackCorrection(
        rejection_event_id=str(row["id"]),
        created_at=str(row["created_at"]),
        project_id=None if project is None or project["project_id"] is None else str(project["project_id"]),
        thread_id=owner,
    )


def record_sent_back_correction(fact: SentBackCorrection, *, db=None) -> bool:
    """Write a prepared sent-back fact once, with no reads under ``BEGIN IMMEDIATE``."""
    from infra.database import write_transaction
    from infra.outcome_events import record_outcome_event

    attributes = {
        "source_kind": SOURCE_KIND_SENT_BACK,
        "source_id": fact.rejection_event_id,
        "thread_id": fact.thread_id,
        "label": LABEL_CORRECTION,
    }
    with write_transaction(db, scope="record_sent_back_correction") as connection:
        result = record_outcome_event(
            connection,
            dedupe_key=f"correction:{SOURCE_KIND_SENT_BACK}:{fact.rejection_event_id}",
            event_name=CORRECTION_EVENT,
            source="corsia",
            provenance="observed",
            ts=fact.created_at,
            subject_key=subject_key(SOURCE_KIND_SENT_BACK, fact.rejection_event_id),
            project_id=fact.project_id,
            agent_thread_id=fact.thread_id,
            attributes=attributes,
        )
        if result.dropped_keys:
            raise RuntimeError("sent-back correction fact attributes were dropped")
        return result.inserted


def record_rejection_correction(rejection_event_id: str, *, db) -> bool:
    """Prepare outside the write transaction, then record the rejection fact."""
    fact = prepare_sent_back_correction(rejection_event_id, db=db)
    return record_sent_back_correction(fact, db=db) if fact is not None else False


def record_rejection_corrections(rejection_event_ids: list[str], *, db) -> None:
    """Record committed reviewed-node rejections; a failure is logged and left to the nightly recovery."""
    for rejection_event_id in rejection_event_ids:
        try:
            record_rejection_correction(rejection_event_id, db=db)
        except Exception:
            log.warning(
                "sent-back correction recording failed for rejection event %s; the nightly recovery retries it",
                rejection_event_id,
                exc_info=True,
            )


def schedule_rejection_corrections(connection, rejection_event_ids: list[str]) -> None:
    """Record the facts once the write open on ``connection`` commits.

    The commit is the boundary's, so no read runs under its ``BEGIN IMMEDIATE``.
    A write inside a transaction nobody here owns has no commit to wait for and
    records in place.
    """
    from infra.database import TransactionOwnershipError
    from infra.write_effects import after_commit

    def record() -> None:
        record_rejection_corrections(rejection_event_ids, db=connection)

    try:
        after_commit(connection, record)
    except TransactionOwnershipError:
        record()


def unrecorded_rejection_event_ids(db, *, since: str | None, limit: int) -> list[str]:
    """Select reviewed-node rejections that have no fact yet, oldest first, at most ``limit``.

    A rejection creates its revision task (``follow_up_of`` the reviewed node,
    with a ``revision_created`` event) in the same committed mutation; returned
    worker leaves get the same feedback event but no revision of their own. A
    worker reviewing its own sub-worker does create a revision in the worker
    plan, so it remains a verdict. The fact's identity is the rejection event
    id, the same key the live path writes, so a recorded event never reappears.
    """
    clauses = ["rejected.event='review_rejected'"]
    params: list[str | int] = []
    if since is not None:
        clauses.append("rejected.created_at>=?")
        params.append(since)
    params.append(limit)
    rows = db.execute(
        """SELECT rejected.id
             FROM plan_node_events AS rejected
            WHERE """ + " AND ".join(clauses) + """
              AND EXISTS (SELECT 1 FROM plan_nodes AS revision
                            JOIN plan_node_events AS created ON created.node_id=revision.id
                             AND created.event='revision_created'
                           WHERE revision.follow_up_of=rejected.node_id)
              AND NOT EXISTS (SELECT 1 FROM outcome_events AS fact
                               WHERE fact.dedupe_key='correction:sent_back:' || rejected.id)
            ORDER BY rejected.created_at, rejected.rowid LIMIT ?""",
        params,
    ).fetchall()
    return [str(row["id"]) for row in rows]


def _scan_start(db, window: timedelta) -> str | None:
    """The oldest rejection time the recovery rescans: the newest recorded fact minus ``window``.

    None while no sent-back fact exists, so the first runs also recover the
    history from before this recorder shipped; the batch limit spreads that
    over successive nights.
    """
    newest = db.execute(
        "SELECT MAX(ts) AS ts FROM outcome_events WHERE event_name=? AND subject_key LIKE ?",
        (CORRECTION_EVENT, SENT_BACK_SUBJECT_PREFIX + "%"),
    ).fetchone()["ts"]
    if newest is None:
        return None
    return (datetime.fromisoformat(newest) - window).isoformat()


def reconcile_sent_back_corrections(
    db, *, window: timedelta = RECONCILE_WINDOW, limit: int = RECONCILE_BATCH,
) -> int:
    """Record the sent-back facts the live path missed; safe to repeat by event-id dedupe."""
    event_ids = unrecorded_rejection_event_ids(db, since=_scan_start(db, window), limit=limit)
    recorded = 0
    for event_id in event_ids:
        try:
            recorded += record_rejection_correction(event_id, db=db)
        except Exception:
            log.warning("sent-back correction recovery failed for rejection event %s", event_id, exc_info=True)
    return recorded
