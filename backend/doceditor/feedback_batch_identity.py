"""Canonical feedback batch identity, including isolated review requests."""

from __future__ import annotations

import uuid


def open_batch(db, *, author_kind, author_id, artifact_id, recipient_id, now,
               closes_at, review_request_id=None) -> str:
    if review_request_id is not None:
        request = db.execute(
            "SELECT * FROM doc_review_requests WHERE id=?", (review_request_id,),
        ).fetchone()
        from doceditor.review_lifecycle import is_open

        if (request is None or not is_open(db, request)
                or author_kind != "agent" or request["reviewer_thread_id"] != author_id
                or request["artifact_id"] != artifact_id):
            raise ValueError("review fork no longer owns this feedback")
    row = db.execute(
        """SELECT id FROM doc_feedback_batches
           WHERE author_kind=? AND author_id=? AND artifact_id=?
             AND recipient_id=? AND review_request_id IS ? AND state='open'""",
        (author_kind, author_id, artifact_id, recipient_id, review_request_id),
    ).fetchone()
    if row is not None:
        return str(row["id"])
    batch_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO doc_feedback_batches
           (id,artifact_id,author_kind,author_id,recipient_id,review_request_id,state,
            opened_at,last_item_at,closes_at,item_count,created_at,updated_at)
           VALUES (?,?,?,?,?,?,'open',?,?,?,0,?,?)""",
        (batch_id, artifact_id, author_kind, author_id, recipient_id, review_request_id,
         now, now, closes_at, now, now),
    )
    return batch_id
