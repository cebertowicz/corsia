"""Transaction-local staging effects invoked by the review request lifecycle."""

from __future__ import annotations

import logging

from doceditor.feedback_batch_delivery import claim_and_deliver
from infra.passive_changes import append_thread_context_note

log = logging.getLogger(__name__)


def drop_unsent_feedback(db, request_id: str) -> None:
    """Drop withdrawn request staging without changing visible comments."""
    unsent = (
        "SELECT id FROM doc_feedback_batches "
        "WHERE review_request_id=? AND state IN ('open','undelivered')"
    )
    db.execute(f"DELETE FROM doc_feedback_batch_items WHERE batch_id IN ({unsent})", (request_id,))
    db.execute(f"DELETE FROM doc_feedback_edit_items WHERE batch_id IN ({unsent})", (request_id,))
    db.execute(
        "DELETE FROM doc_feedback_batches WHERE review_request_id=? AND state IN ('open','undelivered')",
        (request_id,),
    )


def deliver_scoped(db, request_id: str) -> tuple[bool, list[str], list[dict]]:
    """Deliver only a request's batches, inside its submitted transition."""
    from registry import models

    rows = db.execute(
        "SELECT id,state FROM doc_feedback_batches WHERE review_request_id=? ORDER BY opened_at,id",
        (request_id,),
    ).fetchall()
    pending = any(row["state"] in {"open", "undelivered"} for row in rows)
    woken: list[str] = []
    for row in rows:
        if row["state"] in {"open", "undelivered"}:
            _closed, selected = claim_and_deliver(
                db, str(row["id"]), from_state=row["state"],
                reason="submit" if row["state"] == "open" else None,
            )
            woken.extend(selected)
    undelivered = [dict(row) for row in db.execute(
        "SELECT * FROM doc_feedback_batches WHERE review_request_id=? "
        "AND state='undelivered' ORDER BY updated_at,id", (request_id,),
    )]
    for batch in undelivered:
        log.warning("artifact review feedback undelivered request_id=%s reason=%s",
                    request_id, batch["undelivered_reason"])
    request = db.execute("SELECT artifact_id FROM doc_review_requests WHERE id=?", (request_id,)).fetchone()
    artifact = models.get_artifact(request["artifact_id"], db=db)
    if artifact is not None:
        answered = db.execute(
            "SELECT id FROM doc_feedback_batches WHERE review_request_id=? "
            "AND state='delivered' AND delivery_outcome IN ('node','ui') AND recipient_id=? "
            "ORDER BY opened_at,id LIMIT 1", (request_id, artifact["parent_thread_id"]),
        ).fetchone()
        if answered is not None:
            db.execute("UPDATE doc_review_requests SET answered_batch_id=? WHERE id=?",
                       (answered["id"], request_id))
    return pending, woken, undelivered


def submit_fork_feedback(db, request_id: str, reviewer_thread_id: str,
                         artifact_id: str) -> tuple[bool, list[str], list[dict]]:
    """Submit only this fork's feedback and leave its reviewer asleep."""
    from doceditor.review_lifecycle import notify_change, transition
    from registry import models

    request = db.execute("SELECT * FROM doc_review_requests WHERE id=?", (request_id,)).fetchone()
    if request is None or request["reviewer_thread_id"] != reviewer_thread_id:
        raise PermissionError("review fork no longer owns this request")
    if request["artifact_id"] != artifact_id or request["state"] == "withdrawn":
        raise ValueError("review request is no longer open")
    if request["state"] == "submitted":
        return False, [], []
    if request["state"] != "forked":
        raise ValueError("review fork no longer owns this request")
    result = transition(db, request_id, "submitted")
    comments = db.execute(
        "SELECT DISTINCT c.id,c.body FROM doc_comments c "
        "JOIN doc_feedback_batch_items i ON i.comment_id=c.id "
        "JOIN doc_feedback_batches b ON b.id=i.batch_id "
        "WHERE b.review_request_id=? ORDER BY c.id LIMIT 3", (request_id,),
    ).fetchall()
    summary = "; ".join(str(row["body"])[:200] for row in comments) or "no comments were left"
    artifact = models.get_artifact(artifact_id, db=db)
    if artifact is not None:
        notify_change(db, request, artifact)
    append_thread_context_note(
        db, thread_id=reviewer_thread_id,
        summary=f"Document review submitted for {artifact_id}: {summary}",
        details={"kind": "artifact_review_receipt", "request_id": request_id,
                 "artifact_id": artifact_id},
    )
    return result.pending, list(result.woken), result.undelivered
