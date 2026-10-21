"""The ordinary reviewer send, with admission for a disposable review fork."""

from __future__ import annotations

from doceditor import feedback_batches, feedback_state
from doceditor.review_lifecycle import admit_feedback, submit_without_batch


def send_document_feedback(db, reviewer_id: str, artifact_id: str) -> tuple[bool, list[str], list[dict]]:
    """Send every open batch and retry pending delivery, just as the reviewer does."""
    identity = dict(author_kind="agent", author_id=reviewer_id, artifact_id=artifact_id)
    pending = feedback_state.has_pending_for_document(db, **identity)
    woken = feedback_batches.close_for_document(db, **identity, reason="submit")
    woken.extend(feedback_batches.retry_for_document(db, **identity))
    undelivered = feedback_state.undelivered_for_document(db, **identity)
    if not undelivered:
        woken.extend(submit_without_batch(db, artifact_id, reviewer_id).woken)
    return pending, woken, undelivered


def submit_fork_feedback(db, request_id: str, reviewer_thread_id: str,
                         artifact_id: str) -> tuple[bool, list[str], list[dict]]:
    """Check the fork's ownership, then use the reviewer's ordinary send."""
    request = db.execute("SELECT * FROM doc_review_requests WHERE id=?", (request_id,)).fetchone()
    if request is None or request["reviewer_thread_id"] != reviewer_thread_id:
        raise PermissionError("review fork no longer owns this request")
    if request["artifact_id"] != artifact_id or request["state"] == "withdrawn":
        raise ValueError("review request is no longer open")
    if request["state"] == "submitted":
        return False, [], []
    admit_feedback(db, request_id, author_kind="agent", author_id=reviewer_thread_id, artifact_id=artifact_id)
    return send_document_feedback(db, reviewer_thread_id, artifact_id)
