"""Plan-owned state queries and settlement for document-feedback delivery."""

from __future__ import annotations

from plans import review_requests, service
from plans.plan_write import transaction

PROVENANCE = "document.feedback_batch"
STOPPED_NOTE = "{evidence} This step was stopped then, so it did not settle by itself; mark it done once it resumes."


def _existing(db, recipient_thread_id: str, batch_id: str, provenance: str = PROVENANCE) -> str | None:
    row = db.execute(
        """SELECT l.node_id FROM plan_node_artifact_links l
           JOIN plan_nodes n ON n.id=l.node_id
           JOIN plans p ON p.id=n.plan_id
           WHERE p.owner_kind='thread' AND p.owner_id=?
             AND l.provenance=? AND l.source_event_id=?
           ORDER BY l.created_at LIMIT 1""",
        (recipient_thread_id, provenance, batch_id),
    ).fetchone()
    return str(row["node_id"]) if row else None


def settle_feedback_node(db, recipient_thread_id: str, batch_id: str, *, evidence: str) -> None:
    """Settle a feedback task through the shared document-delivery transition."""
    node_id = _existing(db, recipient_thread_id, batch_id)
    if node_id is None:
        return
    with transaction(db) as connection:
        review_requests.settle_node(
            connection,
            service._node_row(connection, node_id),
            evidence=evidence,
            provenance=PROVENANCE,
            verified_by="system:document.feedback_batch",
            stopped_note=STOPPED_NOTE,
            stopped_event="feedback_settled_while_stopped",
        )


def unstarted_feedback_batch_id(
    db,
    *,
    recipient_thread_id: str,
    author_kind: str,
    author_id: str,
    artifact_id: str,
) -> str | None:
    """Return the newest delivered batch whose recipient has not started its task."""
    row = db.execute(
        """SELECT batch.id FROM doc_feedback_batches batch
           JOIN plan_nodes node ON node.id=batch.delivered_node_id AND node.state='active'
           JOIN plans plan ON plan.id=node.plan_id
           WHERE batch.author_kind=? AND batch.author_id=? AND batch.artifact_id=?
             AND batch.recipient_id=? AND batch.state='delivered'
             AND batch.delivery_outcome='node'
             AND plan.owner_kind='thread' AND plan.owner_id=?
             AND NOT EXISTS (
                 SELECT 1 FROM active_turns turn
                 WHERE turn.thread_id=? AND turn.plan_node_id=node.id
                   AND turn.status IN ('running','waiting_retry')
             )
           ORDER BY batch.updated_at DESC,batch.id DESC LIMIT 1""",
        (
            author_kind, author_id, artifact_id, recipient_thread_id,
            recipient_thread_id, recipient_thread_id,
        ),
    ).fetchone()
    return str(row["id"]) if row is not None else None
