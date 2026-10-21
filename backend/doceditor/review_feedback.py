"""Explicit fork submission and its passive reviewer receipt."""

from __future__ import annotations

from infra.passive_changes import append_thread_context_note


def submit_fork_feedback(db, request_id: str, reviewer_thread_id: str,
                         artifact_id: str) -> tuple[bool, list[str], list[dict]]:
    """Submit this fork's comments and leave its reviewer asleep."""
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
    comments = db.execute(
        "SELECT c.id,c.body FROM doc_comments c "
        "JOIN doc_review_request_comments s ON s.comment_id=c.id "
        "WHERE s.request_id=? ORDER BY c.id LIMIT 3", (request_id,),
    ).fetchall()
    result = transition(db, request_id, "submitted")
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
