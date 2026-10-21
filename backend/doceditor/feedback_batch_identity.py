"""One open feedback batch per author, document and recipient."""

from __future__ import annotations

import uuid


def open_batch(db, *, author_kind, author_id, artifact_id, recipient_id, now,
               closes_at) -> str:
    row = db.execute(
        """SELECT id FROM doc_feedback_batches
           WHERE author_kind=? AND author_id=? AND artifact_id=?
             AND recipient_id=? AND state='open'""",
        (author_kind, author_id, artifact_id, recipient_id),
    ).fetchone()
    if row is not None:
        return str(row["id"])
    batch_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO doc_feedback_batches
           (id,artifact_id,author_kind,author_id,recipient_id,state,
            opened_at,last_item_at,closes_at,item_count,created_at,updated_at)
           VALUES (?,?,?,?,?,'open',?,?,?,0,?,?)""",
        (batch_id, artifact_id, author_kind, author_id, recipient_id,
         now, now, closes_at, now, now),
    )
    return batch_id
