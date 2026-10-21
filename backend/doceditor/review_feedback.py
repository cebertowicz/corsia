"""Request-scoped document review submission, receipt and withdrawal."""

from __future__ import annotations

from doceditor.feedback_batch_delivery import claim_and_deliver
from doceditor.review_lifecycle import is_open, notify_change, transition
from infra.passive_changes import append_thread_context_note


def drop_unsent_feedback(db, request_id: str) -> None:
    """Drop a request's unsent batch, item and edit staging; never touch comments.

    Every review that ends without a submit (withdrawn, or a continuation that
    ended) follows this one rule. Comments are visible before delivery and may
    already have replies, so their open/resolved and acknowledgement state stays
    as their authors and readers left it. A batch has no withdrawn state, so its
    rows go and it can never deliver.
    """
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


def close_ended_continuations(db) -> None:
    """Drop unsent staging after its continuing task ends without submission.

    Comments are already visible independently of batch delivery. Preserve their
    open/resolved and acknowledgement state without delivery or a reviewer wake.
    """
    from registry import models

    rows = db.execute(
        "SELECT r.* FROM doc_review_requests r WHERE r.state IN ('handed_back','failed') "
        "AND EXISTS (SELECT 1 FROM doc_feedback_batches b WHERE b.review_request_id=r.id "
        "AND b.state IN ('open','undelivered'))",
    ).fetchall()
    for row in rows:
        if is_open(db, row):
            continue
        drop_unsent_feedback(db, row["id"])
        artifact = models.get_artifact(row["artifact_id"], db=db)
        if artifact is not None:
            notify_change(db, row, artifact)


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
            _closed, selected = claim_and_deliver(
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
