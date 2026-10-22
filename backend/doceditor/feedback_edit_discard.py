"""Leaf mutation: drop the edit feedback a write of accepted suggestions just made."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from doceditor.feedback_state import USER_AUTHOR_ID


def discard_new_edit_item(db, artifact_id: str, text: str) -> int:
    """Remove the user's open edit item that the one write ending in ``text`` created.

    The review bar's "Accept N suggestions and approve" writes the document for the person,
    but those words are the helpers', not the person's own feedback. An item made by exactly
    one save with no earlier segment is wholly that write; if the person had edited before,
    their item holds a second save and stays. A batch left with nothing is deleted.
    """
    rows = db.execute(
        """SELECT batch.id FROM doc_feedback_batches batch
           JOIN doc_feedback_edit_items item ON item.batch_id=batch.id
           WHERE batch.state='open' AND batch.author_kind='user' AND batch.author_id=?
             AND batch.artifact_id=? AND item.save_count=1 AND item.prior_segments_diff=''
             AND item.latest_sha256=?""",
        (USER_AUTHOR_ID, artifact_id, hashlib.sha256(text.encode("utf-8")).hexdigest()),
    ).fetchall()
    for row in rows:
        batch_id = str(row["id"])
        db.execute("DELETE FROM doc_feedback_edit_items WHERE batch_id=?", (batch_id,))
        remaining = db.execute(
            "SELECT COUNT(*) FROM doc_feedback_batch_items WHERE batch_id=?", (batch_id,)
        ).fetchone()[0]
        if remaining:
            db.execute(
                "UPDATE doc_feedback_batches SET item_count=?,updated_at=? WHERE id=?",
                (remaining, datetime.now(timezone.utc).isoformat(), batch_id),
            )
        else:
            db.execute("DELETE FROM doc_feedback_batches WHERE id=?", (batch_id,))
    return len(rows)
