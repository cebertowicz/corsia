"""Immutable correction facts observed from plan review rejections.

The rejection event retains the feedback.  This projection keeps only its stable
identifier, so Measurement can count sent-backs without duplicating review text
or sending a correction note back to the worker.  The fact is written in the
rejection's own transaction, so a rejection and its fact stand or fall together.
"""

from __future__ import annotations

from core.corrections_recording import (
    CORRECTION_EVENT,
    LABEL_CORRECTION,
    SOURCE_KIND_SENT_BACK,
    sent_back_dedupe_key,
    subject_key,
)


def _attribution(executor: str | None, db) -> tuple[str | None, str | None]:
    """The thread a sent-back counts against, and its project.

    The nearest agent at or above the executor; a worker with no agent above it
    (or a missing thread row) keeps the executor itself, so the fact is never dropped.
    """
    from infra.queries import thread_is_temporary

    current, first, seen = executor, None, set()
    while current is not None and current not in seen:
        seen.add(current)
        row = db.execute(
            "SELECT id,spawned_by,agent_enabled,project_id FROM threads WHERE id=?", (current,),
        ).fetchone()
        if row is None:
            break
        first = first or row
        if not thread_is_temporary(row):
            return str(row["id"]), row["project_id"]
        current = row["spawned_by"]
    return executor, None if first is None else first["project_id"]


def record_rejection_correction(db, rejection_event_id: str) -> bool:
    """Write the correction fact for one reviewed-node rejection; True when newly recorded.

    Used by the rejection itself and by the migration that records earlier ones, so both
    attribute alike. Raises on any failure: inside the rejection, that fails the rejection.
    """
    from infra.outcome_events import record_outcome_event
    from plans.public import executor_of

    row = db.execute(
        """SELECT e.created_at,n.executor_thread_id,n.owner_thread_id,
                  p.owner_kind,p.owner_id
             FROM plan_node_events e
             JOIN plan_nodes n ON n.id=e.node_id
             JOIN plans p ON p.id=n.plan_id
            WHERE e.id=? AND e.event='review_rejected'""",
        (rejection_event_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no review rejection {rejection_event_id}")
    thread_id, project_id = _attribution(executor_of(row, row), db)
    attributes = {
        "source_kind": SOURCE_KIND_SENT_BACK, "source_id": rejection_event_id,
        "label": LABEL_CORRECTION,
    }
    if thread_id is not None:
        attributes["thread_id"] = thread_id
    result = record_outcome_event(
        db,
        dedupe_key=sent_back_dedupe_key(rejection_event_id),
        event_name=CORRECTION_EVENT,
        source="corsia",
        provenance="observed",
        ts=str(row["created_at"]),
        subject_key=subject_key(SOURCE_KIND_SENT_BACK, rejection_event_id),
        project_id=None if project_id is None else str(project_id),
        agent_thread_id=thread_id,
        attributes=attributes,
    )
    if result.dropped_keys:
        raise RuntimeError("sent-back correction fact attributes were dropped")
    return result.inserted
