"""Successful document-feedback completion.

Delivery owns whether a batch reached its recipient. Completion records it,
settles the review requests it answers, and writes the correction notes the
delivery completes in the same transaction (``core.correction_notes``).
"""

from __future__ import annotations

from typing import Any

from doceditor import review_requests


def complete_delivered_batch(db, batch: Any, delivered: Any, now: str, floor: int) -> tuple[str, ...]:
    """Persist a successful delivery; ``floor`` is ``input_floor`` from before the delivery."""
    from core.correction_notes import queue_batch_notes

    if delivered.outcome is None:
        raise RuntimeError("successful feedback delivery has no typed outcome")
    batch_id = str(batch["id"])
    db.execute(
        """UPDATE doc_feedback_batches
           SET delivered_node_id=?,delivery_outcome=?,updated_at=? WHERE id=?""",
        (delivered.node_id, delivered.outcome, now, batch_id),
    )
    db.execute("DELETE FROM doc_feedback_edit_items WHERE batch_id=?", (batch_id,))
    review_woken = review_requests.settle_for_batch(db, batch)
    wakes: list[str] = []
    queue_batch_notes(db, batch_id, floor=floor, wakes=wakes)
    return review_woken + tuple(wakes)
