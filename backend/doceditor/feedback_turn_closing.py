"""Close feedback at the owning reviewer's continuation boundary."""

from __future__ import annotations


def close_for_turn_end(db, thread_id: str) -> list[str]:
    """Close ordinary feedback or review work continued after its hand-back.

    The request's state timestamp is the turn-start floor. ``closes_at``
    remains the quiet-timer deadline on every feedback batch.
    """
    from doceditor.feedback_batches import close_batch

    rows = db.execute(
        """SELECT id FROM doc_feedback_batches
           WHERE state='open' AND author_kind='agent' AND author_id=?
           AND (review_request_id IS NULL OR EXISTS (
               SELECT 1 FROM doc_review_requests r
               WHERE r.id=review_request_id AND r.state IN ('handed_back','failed')
               AND substr(r.state_changed_at,1,26)<=(
                   SELECT substr(started_at,1,26) FROM active_turns WHERE thread_id=?
                   ORDER BY turn_order DESC LIMIT 1
               )
           )) ORDER BY opened_at,id""",
        (thread_id, thread_id),
    ).fetchall()
    woken: list[str] = []
    for row in rows:
        _closed, selected = close_batch(db, row["id"], "turn_end")
        woken.extend(selected)
    return woken
