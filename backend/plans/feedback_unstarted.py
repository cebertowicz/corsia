"""Which document-feedback tasks their recipient has not started, and whether a turn is running."""

from __future__ import annotations


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
             )
           ORDER BY batch.updated_at DESC,batch.id DESC LIMIT 1""",
        (
            author_kind,
            author_id,
            artifact_id,
            recipient_thread_id,
            recipient_thread_id,
            recipient_thread_id,
        ),
    ).fetchone()
    return str(row["id"]) if row is not None else None
