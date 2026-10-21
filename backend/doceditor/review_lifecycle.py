"""One writer for a document review request's lifecycle and staged feedback."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from infra.write_effects import after_commit

FALLBACK_STATES = frozenset({"handed_back", "failed"})
PENDING_STATES = frozenset({"forked", *FALLBACK_STATES})


@dataclass(frozen=True)
class ReviewTransition:
    changed: bool = False
    pending: bool = False
    woken: tuple[str, ...] = ()
    undelivered: list[dict] = field(default_factory=list)


def transition(db, request_id: str, state: str, *, reason: str | None = None) -> ReviewTransition:
    """Change the request and its staging atomically; terminal states win races.

    The caller holds the write transaction. Terminal transitions release staged
    comments to ordinary feedback; withdrawal drops only the staging links.
    """
    from infra.product_events import emit_product_event

    if state not in {"forked", "submitted", "withdrawn", *FALLBACK_STATES}:
        raise ValueError(f"unknown review state: {state}")
    if state in FALLBACK_STATES and not reason:
        raise ValueError("a review hand-back requires a reason")
    source = "state='forked'" if state in FALLBACK_STATES else "state IN ('forked','handed_back','failed')"
    changed = db.execute(
        f"UPDATE doc_review_requests SET state=?,state_changed_at=?,reason=COALESCE(?,reason) "
        f"WHERE id=? AND {source}",
        (state, datetime.now(timezone.utc).isoformat(), reason, request_id),
    ).rowcount
    if not changed:
        return ReviewTransition()
    pending, woken, undelivered = _release_comments(db, request_id, state)
    if state != "forked":
        after_commit(db, lambda: emit_product_event(
            "documents", f"review_{state}", properties={"request_id": request_id, "reason": reason},
        ))
    return ReviewTransition(True, pending, tuple(woken), undelivered)


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


def admit_feedback(db, request_id: str, *, author_kind, author_id, artifact_id) -> None:
    """Refuse feedback unless this request still belongs to its reviewer."""
    request = db.execute(
        "SELECT * FROM doc_review_requests WHERE id=?", (request_id,),
    ).fetchone()
    if (request is None or request["state"] != "forked"
            or author_kind != "agent" or request["reviewer_thread_id"] != author_id
            or request["artifact_id"] != artifact_id):
        raise ValueError("review fork no longer owns this feedback")


def continuing_request(db, artifact_id: str, reviewer_id: str):
    """The latest request is eligible only while its continuation remains open."""
    row = db.execute(
        "SELECT * FROM doc_review_requests WHERE artifact_id=? AND reviewer_thread_id=? "
        "ORDER BY created_at DESC,id DESC LIMIT 1", (artifact_id, reviewer_id),
    ).fetchone()
    return row if row is not None and row["state"] in FALLBACK_STATES and is_open(db, row) else None


def _release_comments(db, request_id: str, state: str) -> tuple[bool, list[str], list[dict]]:
    """Transfer request staging once, using the ordinary item and close paths."""
    from doceditor import feedback_batches
    from registry import models

    if state == "forked":
        return False, [], []
    if state == "withdrawn":
        db.execute("DELETE FROM doc_review_request_comments WHERE request_id=?", (request_id,))
        return False, [], []
    request = db.execute("SELECT * FROM doc_review_requests WHERE id=?", (request_id,)).fetchone()
    artifact = models.get_artifact(request["artifact_id"], db=db)
    staged = db.execute(
        "SELECT c.* FROM doc_comments c JOIN doc_review_request_comments s ON s.comment_id=c.id "
        "WHERE s.request_id=? ORDER BY c.created_at,c.id", (request_id,),
    ).fetchall()
    for comment in staged:
        feedback_batches.join_open_batch(db, dict(comment), artifact["parent_thread_id"])
    db.execute("DELETE FROM doc_review_request_comments WHERE request_id=?", (request_id,))
    if state != "submitted":
        return False, [], []
    batch = db.execute(
        "SELECT id,item_count FROM doc_feedback_batches WHERE author_kind='agent' AND author_id=? "
        "AND artifact_id=? AND recipient_id=? AND state='open'",
        (request["reviewer_thread_id"], request["artifact_id"], artifact["parent_thread_id"]),
    ).fetchone()
    if batch is None:
        return False, [], []
    _closed, woken = feedback_batches.close_batch(db, batch["id"], "submit")
    if batch["item_count"]:
        db.execute("UPDATE doc_review_requests SET answered_batch_id=? WHERE id=?", (batch["id"], request_id))
    failures = [dict(row) for row in db.execute(
        "SELECT * FROM doc_feedback_batches WHERE id=? AND state='undelivered'", (batch["id"],),
    )]
    return True, list(woken), failures


def _submit_continuation(db, row, batch_id: str | None) -> ReviewTransition:
    from plans import public
    from registry import models

    result = transition(db, row["id"], "submitted")
    if not result.changed:
        return result
    if batch_id is not None:
        db.execute("UPDATE doc_review_requests SET answered_batch_id=? WHERE id=?", (batch_id, row["id"]))
    woken = list(result.woken)
    with public.collect_wake_recipients(db, woken):
        evidence = (
            f"Document review comments delivered to the owner in batch {batch_id}."
            if batch_id else "Document review submitted to the owner."
        )
        public.settle_review_node(
            db, row["node_id"], evidence=evidence, provenance="document.review_request",
        )
    artifact = models.get_artifact(row["artifact_id"], db=db)
    if artifact is not None:
        notify_change(db, row, artifact)
    return ReviewTransition(True, result.pending, tuple(woken), result.undelivered)


def submit_without_batch(db, artifact_id: str, reviewer_id: str) -> ReviewTransition:
    """An explicit empty submit can finish an open review continuation."""
    row = continuing_request(db, artifact_id, reviewer_id)
    return _submit_continuation(db, row, None) if row is not None else ReviewTransition()


def settle_for_batch(db, batch) -> tuple[str, ...]:
    """Answer an open continuation only from ordinary feedback delivered to its owner."""
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
    row = continuing_request(db, batch["artifact_id"], batch["author_id"])
    return _submit_continuation(db, row, batch["id"]).woken if row is not None else ()
