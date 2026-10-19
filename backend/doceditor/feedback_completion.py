"""Successful document-feedback completion bridge.

Delivery owns whether a batch reached its recipient. This bridge records the
completion and isolates correction-note bookkeeping so feedback delivery never
depends on a classifier or passive-note failure.
"""

from __future__ import annotations

import logging
from typing import Any

from doceditor import review_requests

log = logging.getLogger(__name__)


def complete_delivered_batch(db, batch: Any, delivered: Any, now: str) -> tuple[str, ...]:
    """Persist a successful delivery and best-effort queue its lone-comment note."""
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
    try:
        from core.correction_notes import queue_feedback_batch_note

        queue_feedback_batch_note(db, batch)
    except Exception:
        log.exception("correction note for delivered feedback batch %s failed", batch_id)
    return review_woken
