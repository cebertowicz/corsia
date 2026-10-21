"""Close ordinary document feedback at its author's turn boundary."""

from __future__ import annotations


def close_for_turn_end(db, thread_id: str) -> list[str]:
    """Close only ordinary batches; request-scoped staging has its own lifecycle."""
    from doceditor.feedback_batches import close_batch

    rows = db.execute(
        """SELECT id FROM doc_feedback_batches
           WHERE state='open' AND author_kind='agent' AND author_id=?
           AND review_request_id IS NULL ORDER BY opened_at,id""",
        (thread_id,),
    ).fetchall()
    woken: list[str] = []
    for row in rows:
        _closed, selected = close_batch(db, row["id"], "turn_end")
        woken.extend(selected)
    return woken
