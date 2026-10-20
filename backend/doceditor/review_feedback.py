"""Request-scoped document review submission, receipt and withdrawal."""

from __future__ import annotations

from doceditor import feedback_batches
from doceditor.review_lifecycle import transition
from infra.passive_changes import append_thread_context_note


def withdraw_fork_feedback(db, request_id: str) -> None:
    """Resolve a withdrawn request's undelivered comments and drop only its batches.

    The comments stay: people may already see them and reply. A batch has no
    withdrawn state, so its batch and item rows go and it can never deliver.
    """
    from doceditor import comments

    rows = db.execute(
        """SELECT DISTINCT item.comment_id FROM doc_feedback_batches batch
           JOIN doc_feedback_batch_items item ON item.batch_id=batch.id
           JOIN doc_comments comment ON comment.id=item.comment_id
           WHERE batch.review_request_id=? AND batch.state IN ('open','undelivered')
             AND comment.resolved=0 ORDER BY item.comment_id""",
        (request_id,),
    ).fetchall()
    for row in rows:
        comments.update(str(row["comment_id"]), {"resolved": True, "acked": True}, db=db)
    db.execute(
        """DELETE FROM doc_feedback_batch_items WHERE batch_id IN (
               SELECT id FROM doc_feedback_batches
               WHERE review_request_id=? AND state IN ('open','undelivered')
           )""",
        (request_id,),
    )
    db.execute(
        "DELETE FROM doc_feedback_batches WHERE review_request_id=? AND state IN ('open','undelivered')",
        (request_id,),
    )


def submit_fork_feedback(db, request_id: str, reviewer_thread_id: str,
                         artifact_id: str) -> tuple[bool, list[str], list[dict]]:
    """Submit only this fork's feedback and leave its reviewer asleep."""
    request = db.execute("SELECT * FROM doc_review_requests WHERE id=?", (request_id,)).fetchone()
    if request is None or request["reviewer_thread_id"] != reviewer_thread_id:
        raise PermissionError("review fork no longer owns this request")
    if request["artifact_id"] != artifact_id or request["state"] == "withdrawn":
        raise ValueError("review request is no longer open")
    if request["state"] == "submitted":
        return False, [], []
    if request["state"] != "forked":
        raise ValueError("review fork no longer owns this request")
    rows = db.execute(
        """SELECT id,state FROM doc_feedback_batches WHERE review_request_id=?
           AND author_kind='agent' AND author_id=? AND artifact_id=? ORDER BY opened_at,id""",
        (request_id, reviewer_thread_id, artifact_id),
    ).fetchall()
    pending = any(row["state"] in {"open", "undelivered"} for row in rows)
    woken: list[str] = []
    for row in rows:
        if row["state"] in {"open", "undelivered"}:
            _closed, selected = feedback_batches._claim_and_deliver(
                db, str(row["id"]), from_state=row["state"],
                reason="submit" if row["state"] == "open" else None,
            )
            woken.extend(selected)
    undelivered = [
        dict(row) for row in db.execute(
            """SELECT * FROM doc_feedback_batches WHERE review_request_id=?
               AND state='undelivered' ORDER BY updated_at,id""", (request_id,),
        )
    ]
    if pending and undelivered:
        return pending, woken, undelivered
    if pending:
        comments = db.execute(
            "SELECT DISTINCT c.id,c.body FROM doc_comments c "
            "JOIN doc_feedback_batch_items i ON i.comment_id=c.id "
            "JOIN doc_feedback_batches b ON b.id=i.batch_id "
            "WHERE b.review_request_id=? ORDER BY c.id LIMIT 3", (request_id,),
        ).fetchall()
        summary = "; ".join(str(row["body"])[:200] for row in comments)
    else:
        transition(db, request_id, "submitted")
        summary = "no comments were left"
    append_thread_context_note(
        db, thread_id=reviewer_thread_id,
        summary=f"Document review submitted for {artifact_id}: {summary}",
        details={"kind": "artifact_review_receipt", "request_id": request_id,
                 "artifact_id": artifact_id},
    )
    return pending, woken, undelivered
