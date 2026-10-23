"""Immutable correction facts observed from plan review rejections.

The rejection event retains the feedback.  This projection keeps only its stable
identifier, so Measurement can count sent-backs without duplicating review text
or sending a correction note back to the worker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.corrections_recording import CORRECTION_EVENT, LABEL_CORRECTION, SOURCE_KIND_SENT_BACK, subject_key

SENT_BACK_FACT_KEYS = ("source_kind", "source_id", "thread_id")

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
        return None
    from plans.public import executor_of

    executor = executor_of(row, {"owner_kind": row["plan_owner_kind"], "owner_id": row["plan_owner_id"]})
    if executor is None:
        return None
    owner = _persistent_owner(executor, db)
    if owner is None:
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
    """Record committed reviewed-node rejections; measurement failures are recoverable."""
    for rejection_event_id in rejection_event_ids:
        try:
            record_rejection_correction(rejection_event_id, db=db)
        except Exception:
            log.warning(
                "sent-back correction recording failed for rejection event %s",
                rejection_event_id,
                exc_info=True,
            )


def sent_back_rejection_event_ids(db, *, since: str | None = None, until: str | None = None) -> list[str]:
    """Select reviewed-node rejections for the one-off idempotent backfill.

    A rejection creates its revision in the same committed mutation.  That
    ``revision_created.payload_json.source_node_id`` is the durable marker for
    the reviewed node; returned worker leaves get the same feedback event but
    create no revision of their own.  A worker reviewing its own sub-worker
    does create a revision in the worker plan, so it remains a verdict.
    """
    clauses = ["rejected.event='review_rejected'"]
    params: list[str] = []
    if since is not None:
        clauses.append("rejected.created_at>=?")
        params.append(since)
    if until is not None:
        clauses.append("rejected.created_at<?")
        params.append(until)
    rows = db.execute(
        """SELECT rejected.id
             FROM plan_node_events AS rejected
             JOIN plan_node_events AS revision ON revision.event='revision_created'
              AND json_extract(revision.payload_json, '$.source_node_id')=rejected.node_id
              AND json_extract(revision.payload_json, '$.feedback')=
                  json_extract(rejected.payload_json, '$.feedback')
              AND revision.actor_id IS rejected.actor_id
             WHERE """ + " AND ".join(clauses) + " ORDER BY rejected.created_at,rejected.rowid",
        params,
    ).fetchall()
    return [str(row["id"]) for row in rows]


def backfill_sent_back_corrections(db, *, since: str | None = None, until: str | None = None) -> int:
    """Record existing rejections one at a time; retries are safe by event-id dedupe."""
    return sum(
        record_rejection_correction(event_id, db=db)
        for event_id in sent_back_rejection_event_ids(db, since=since, until=until)
    )
