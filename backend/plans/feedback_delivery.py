"""Plan-owned state queries and settlement for document-feedback delivery."""

from __future__ import annotations

from plans import review_requests
from plans.plan_write import transaction

PROVENANCE = "document.feedback_batch"
STOPPED_NOTE = (
    "{evidence}\n\nEvery item was decided while this task was paused or held, so it is still open. "
    "Mark it done once it resumes."
)


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
            node_id,
            evidence=evidence,
            provenance=PROVENANCE,
            verified_by="system:document.feedback_batch",
            stopped_note=STOPPED_NOTE,
            stopped_event="feedback_settled_while_stopped",
        )


def feedback_node_has_started(db, recipient_thread_id: str, node_id: str) -> bool:
    """Whether the recipient has ever started a turn for this feedback task."""
    return db.execute(
        "SELECT 1 FROM active_turns WHERE thread_id=? AND plan_node_id=?",
        (recipient_thread_id, node_id),
    ).fetchone() is not None


def recipient_turn_is_active(db, recipient_thread_id: str) -> bool:
    """Whether the recipient is already consuming a running or retrying turn."""
    return db.execute(
        """SELECT 1 FROM active_turns WHERE thread_id=?
           AND status IN ('running','waiting_retry')""",
        (recipient_thread_id,),
    ).fetchone() is not None


def unstarted_feedback_batch_id(
    db,
    *,
    recipient_thread_id: str,
    author_kind: str,
    author_id: str,
    artifact_id: str,
) -> str | None:
    """Return the newest delivered batch whose recipient has never started its task."""
    rows = db.execute(
        """SELECT batch.id,node.id AS node_id FROM doc_feedback_batches batch
           JOIN plan_nodes node ON node.id=batch.delivered_node_id AND node.state='active'
           JOIN plans plan ON plan.id=node.plan_id
           WHERE batch.author_kind=? AND batch.author_id=? AND batch.artifact_id=?
             AND batch.recipient_id=? AND batch.state='delivered'
             AND batch.delivery_outcome='node'
             AND plan.owner_kind='thread' AND plan.owner_id=?
           ORDER BY batch.updated_at DESC,batch.id DESC""",
        (author_kind, author_id, artifact_id, recipient_thread_id, recipient_thread_id),
    ).fetchall()
    for row in rows:
        if not feedback_node_has_started(db, recipient_thread_id, str(row["node_id"])):
            return str(row["id"])
    return None
