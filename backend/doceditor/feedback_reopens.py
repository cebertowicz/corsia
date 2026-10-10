"""Leaf mutations for queued user reopened-comment feedback."""

from __future__ import annotations

from doceditor.feedback_state import USER_AUTHOR_ID


def discard_open_item(db, comment_id: str, now: str) -> None:
    """Remove a re-resolved item from open user batches, retaining delivered history."""
    rows = db.execute(
        """SELECT batch.id FROM doc_feedback_batches batch
           JOIN doc_feedback_batch_items item ON item.batch_id=batch.id
           WHERE batch.state='open' AND batch.author_kind='user' AND batch.author_id=?
             AND item.comment_id=? AND item.event='reopened'""",
        (USER_AUTHOR_ID, comment_id),
    ).fetchall()
    for row in rows:
        batch_id = str(row["id"])
        db.execute(
            "DELETE FROM doc_feedback_batch_items WHERE batch_id=? AND comment_id=? AND event='reopened'",
            (batch_id, comment_id),
        )
        remaining = db.execute(
            "SELECT COUNT(*) FROM doc_feedback_batch_items WHERE batch_id=?", (batch_id,)
        ).fetchone()[0] + db.execute(
            "SELECT COUNT(*) FROM doc_feedback_edit_items WHERE batch_id=?", (batch_id,)
        ).fetchone()[0]
        if remaining:
            db.execute(
                "UPDATE doc_feedback_batches SET item_count=?,updated_at=? WHERE id=?",
                (remaining, now, batch_id),
            )
        else:
            db.execute("DELETE FROM doc_feedback_batches WHERE id=?", (batch_id,))
