"""Claim and deliver one durable document-feedback batch."""

from __future__ import annotations

from typing import Any

from doceditor import feedback_delivery
from doceditor.review_lifecycle import settle_for_batch


def claim_and_deliver(
    db,
    batch_id: str,
    *,
    from_state: str,
    reason: str | None,
    due_by: str | None,
    now: str,
) -> tuple[bool, tuple[str, ...]]:
    """Claim a matching batch, deliver it, and persist either outcome."""
    closed_sql = "closed_reason=?" if reason is not None else "closed_reason=closed_reason"
    due_sql = " AND closes_at<=?" if due_by is not None else ""
    params: list[Any] = [now]
    if reason is not None:
        params.append(reason)
    params.extend([batch_id, from_state])
    if due_by is not None:
        closed_sql = "closed_reason=COALESCE(NULLIF(closed_reason,''),CASE author_kind WHEN 'agent' THEN 'quiet' ELSE 'idle' END)"
        params.append(due_by)
    changed = db.execute(
        f"""UPDATE doc_feedback_batches SET state='delivered',updated_at=?,
            {closed_sql},undelivered_reason=NULL,attempts=attempts+1
            WHERE id=? AND state=?{due_sql}""",
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
        review_woken = settle_for_batch(db, batch)
    return True, delivered.woken_threads + review_woken
