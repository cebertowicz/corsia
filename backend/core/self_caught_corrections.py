"""Project thread-authored plan corrections into immutable Measurement facts."""

from __future__ import annotations

from core.correction_owner import attribution
from core.corrections_recording import (
    CORRECTION_EVENT, LABEL_CORRECTION, SENT_BACK_SUBJECT_PREFIX,
    SOURCE_KIND_SELF_CAUGHT, subject_key,
)


def record_self_caught_correction(db, event_id: str) -> bool:
    """Record a thread-authored correction unless its work was already sent back."""
    from infra.outcome_events import record_outcome_event

    row = db.execute(
        """SELECT e.created_at,e.actor_id,n.follow_up_of,n.commitment_id
             FROM plan_node_events e JOIN plan_nodes n ON n.id=e.node_id
             JOIN threads t ON t.id=e.actor_id
            WHERE e.id=? AND e.event='correction_created'""", (event_id,),
    ).fetchone()
    if row is None:
        return False
    duplicate = db.execute(
        """SELECT 1 FROM plan_node_events r
             JOIN plan_nodes n ON n.id=r.node_id
             JOIN outcome_events f ON f.subject_key=? || r.id
            WHERE r.event='review_rejected' AND f.event_name=? AND f.retracted_at IS NULL
              AND json_extract(f.attributes,'$.source_kind')='sent_back'
              AND (n.id=? OR (? IS NOT NULL AND n.commitment_id=?)) LIMIT 1""",
        (SENT_BACK_SUBJECT_PREFIX, CORRECTION_EVENT, row["follow_up_of"],
         row["commitment_id"], row["commitment_id"]),
    ).fetchone()
    if duplicate:
        return False
    thread_id, project_id = attribution(row["actor_id"], db)
    attributes = {"source_kind": SOURCE_KIND_SELF_CAUGHT, "source_id": event_id,
                  "label": LABEL_CORRECTION}
    if thread_id is not None:
        attributes["thread_id"] = thread_id
    result = record_outcome_event(
        db, dedupe_key=f"correction:{SOURCE_KIND_SELF_CAUGHT}:{event_id}",
        event_name=CORRECTION_EVENT, source="corsia", provenance="observed",
        ts=str(row["created_at"]), subject_key=subject_key(SOURCE_KIND_SELF_CAUGHT, event_id),
        project_id=None if project_id is None else str(project_id),
        agent_thread_id=thread_id, attributes=attributes,
    )
    if result.dropped_keys:
        raise RuntimeError("self-caught correction fact attributes were dropped")
    return result.inserted
