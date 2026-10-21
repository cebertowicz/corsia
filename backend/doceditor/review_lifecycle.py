"""One writer for a document review request's explicit lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone

FALLBACK_STATES = frozenset({"handed_back", "failed"})
PENDING_STATES = frozenset({"forked", *FALLBACK_STATES})


def transition(db, request_id: str, state: str, *, reason: str | None = None) -> bool:
    """Move an unfinished request once; terminal submission/withdrawal wins races.

    The caller holds the request's write transaction, including any Plans or
    feedback effect. A failed fork can subsequently submit via its live task;
    its failure reason remains available after that submission.
    """
    if state not in {"forked", "submitted", "withdrawn", *FALLBACK_STATES}:
        raise ValueError(f"unknown review state: {state}")
    if state in FALLBACK_STATES and not reason:
        raise ValueError("a review hand-back requires a reason")
    source = "state='forked'" if state in FALLBACK_STATES else "state IN ('forked','handed_back','failed')"
    return bool(db.execute(
        f"UPDATE doc_review_requests SET state=?,state_changed_at=?,reason=COALESCE(?,reason) "
        f"WHERE id=? AND {source}",
        (state, datetime.now(timezone.utc).isoformat(), reason, request_id),
    ).rowcount)


def is_open(db, row) -> bool:
    """A fork is pending; a handed-back request follows its public Plans task."""
    if row["state"] == "forked":
        return True
    if row["state"] in FALLBACK_STATES:
        from plans import public

        try:
            return public.review_is_open(db, row["node_id"])
        except public.PlanError:
            return False  # A deleted continuation cannot receive document changes.
    return False


def notify_change(db, request, artifact) -> None:
    """Invalidate the request's affected readers after its transaction commits."""
    from infra.write_effects import after_commit
    from plans import public
    from registry.change_notifications import build_artifact_change_messages

    affected = [request["reviewer_thread_id"], artifact["parent_thread_id"]]
    if request["requester_kind"] == "agent":
        affected.append(request["requester_id"])
    messages = build_artifact_change_messages(
        request["artifact_id"], owner_thread_id=artifact["parent_thread_id"],
        affected_thread_ids=affected, db=db,
    )
    after_commit(db, lambda: public.deliver_notifications(messages))


def settle_for_batch(db, batch) -> tuple[str, ...]:
    """Answer a request only from delivered feedback to the document owner."""
    from plans import public
    from registry import models

    if batch["author_kind"] != "agent":
        return ()
    delivered = db.execute(
        "SELECT state,delivery_outcome FROM doc_feedback_batches WHERE id=?", (batch["id"],),
    ).fetchone()
    if (delivered is None or delivered["state"] != "delivered"
            or delivered["delivery_outcome"] not in {"node", "ui"}):
        return ()
    artifact = models.get_artifact(batch["artifact_id"], db=db)
    if artifact is None or batch["recipient_id"] != artifact["parent_thread_id"]:
        return ()
    request_id = batch["review_request_id"]
    if request_id:
        row = db.execute("SELECT * FROM doc_review_requests WHERE id=?", (request_id,)).fetchone()
    else:
        row = db.execute(
            "SELECT * FROM doc_review_requests WHERE artifact_id=? AND reviewer_thread_id=? "
            "ORDER BY created_at DESC,id DESC LIMIT 1", (batch["artifact_id"], batch["author_id"]),
        ).fetchone()
    if row is None or row["state"] not in FALLBACK_STATES:
        return ()
    still_open = row["state"] in FALLBACK_STATES and is_open(db, row)
    transition(db, row["id"], "submitted")
    db.execute("UPDATE doc_review_requests SET answered_batch_id=? WHERE id=?", (batch["id"], row["id"]))
    woken: list[str] = []
    if still_open:
        with public.collect_wake_recipients(db, woken):
            public.settle_review_node(
                db, row["node_id"],
                evidence=f"Document review comments delivered to the owner in batch {batch['id']}.",
                provenance="document.review_request",
            )
    notify_change(db, row, artifact)
    return tuple(woken)
