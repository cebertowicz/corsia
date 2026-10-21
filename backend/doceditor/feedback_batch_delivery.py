"""Public transaction-local delivery seam for document-feedback batch state."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from doceditor import feedback_delivery, review_lifecycle


def claim_and_deliver(db, batch_id: str, *, from_state: str,
                      reason: str | None) -> tuple[bool, tuple[str, ...]]:
    """Claim one batch and settle its request in the caller's write transaction."""
    batch = db.execute(
        "SELECT * FROM doc_feedback_batches WHERE id=? AND state=?", (batch_id, from_state),
    ).fetchone()
    if batch is None:
        return False, ()
    review_lifecycle.prepare_for_batch(db, batch)
    now = datetime.now(timezone.utc).isoformat()
    closed_sql = "closed_reason=?" if reason is not None else "closed_reason=closed_reason"
    params: list[Any] = [now]
    if reason is not None:
        params.append(reason)
    params.extend([batch_id, from_state])
    changed = db.execute(
        f"""UPDATE doc_feedback_batches SET state='delivered',updated_at=?,
            {closed_sql},undelivered_reason=NULL,attempts=attempts+1
            WHERE id=? AND state=?""",
        params,
    )
    if changed.rowcount == 0:
        return False, ()
    batch = db.execute("SELECT * FROM doc_feedback_batches WHERE id=?", (batch_id,)).fetchone()
    delivered = feedback_delivery.deliver_claimed(db, batch)
    review_woken: tuple[str, ...] = ()
    if delivered.reason is not None:
        db.execute(
            """UPDATE doc_feedback_batches
               SET state='undelivered',undelivered_reason=?,delivery_outcome=NULL,
                   updated_at=? WHERE id=?""",
            (delivered.reason, now, batch_id),
        )
    else:
        if delivered.outcome is None:
            raise RuntimeError("successful feedback delivery has no typed outcome")
        db.execute(
            """UPDATE doc_feedback_batches
               SET delivered_node_id=?,delivery_outcome=?,updated_at=? WHERE id=?""",
            (delivered.node_id, delivered.outcome, now, batch_id),
        )
        db.execute("DELETE FROM doc_feedback_edit_items WHERE batch_id=?", (batch_id,))
        if batch["review_request_id"] is None:
            review_woken = review_lifecycle.settle_for_batch(db, batch)
    return True, delivered.woken_threads + review_woken
