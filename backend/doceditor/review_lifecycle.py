"""One writer for a document review request's lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from infra.write_effects import after_commit

FALLBACK_STATES = frozenset({"handed_back", "failed"})


@dataclass(frozen=True)
class ReviewTransition:
    changed: bool = False
    woken: tuple[str, ...] = ()


def transition(db, request_id: str, state: str, *, reason: str | None = None) -> ReviewTransition:
    """Change only the request; terminal states win races in the write transaction."""
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
    if state != "forked":
        after_commit(db, lambda: emit_product_event(
            "documents", f"review_{state}", properties={"request_id": request_id, "reason": reason},
        ))
    return ReviewTransition(True)


def is_open(db, row) -> bool:
    """A fork is pending; a handed-back request follows its public Plans task."""
    if row["state"] == "forked":
        return True
    if row["state"] in FALLBACK_STATES:
        from plans import public

        try:
            return public.review_is_open(db, row["node_id"])
        except public.PlanError as exc:
            if str(exc).startswith("unknown plan node:"):
                return False  # A deleted continuation cannot receive document changes.
            raise
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


def pending_request(db, artifact_id: str, reviewer_id: str):
    """Only the latest request can be answered, while its fork or task is open."""
    row = db.execute(
        "SELECT * FROM doc_review_requests WHERE artifact_id=? AND reviewer_thread_id=? "
        "ORDER BY created_at DESC,id DESC LIMIT 1", (artifact_id, reviewer_id),
    ).fetchone()
    return row if row is not None and is_open(db, row) else None


def _submit_request(db, row, batch_id: str | None) -> ReviewTransition:
    from plans import public
    from registry import models

    result = transition(db, row["id"], "submitted")
    if not result.changed:
        return result
    if batch_id is not None:
        db.execute("UPDATE doc_review_requests SET answered_batch_id=? WHERE id=?", (batch_id, row["id"]))
    woken: list[str] = []
    if row["node_id"] is not None:
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
    if row["state"] == "forked":
        _receipt(db, row, batch_id)
    return ReviewTransition(True, tuple(woken))


def request_comment_count(db, row) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM doc_comments WHERE artifact_id=? AND author_kind='agent' "
        "AND author_thread_id=? AND created_at>=?",
        (row["artifact_id"], row["reviewer_thread_id"], row["created_at"]),
    ).fetchone()[0]


def _receipt(db, row, batch_id: str | None) -> None:
    from infra.passive_changes import append_thread_context_note

    count = request_comment_count(db, row)
    summary = "nothing was sent to the document owner"
    if batch_id is not None:
        comments = db.execute(
            "SELECT c.body FROM doc_feedback_batch_items i JOIN doc_comments c ON c.id=i.comment_id "
            "WHERE i.batch_id=? ORDER BY c.created_at,c.id LIMIT 3", (batch_id,),
        ).fetchall()
        summary = f"{count} review comments written; feedback was delivered"
        if comments:
            summary += ": " + "; ".join(str(comment["body"])[:200] for comment in comments)
    append_thread_context_note(
        db, thread_id=row["reviewer_thread_id"],
        summary=f"Document review submitted for {row['artifact_id']}: {summary}",
        details={"kind": "artifact_review_receipt", "request_id": row["id"],
                 "artifact_id": row["artifact_id"], "item_count": count},
    )


def submit_without_batch(
    db, artifact_id: str, reviewer_id: str, batch_id: str | None = None, *, fork_end: bool = False,
) -> ReviewTransition:
    """Finish a continuation, or a fork that has reached its own end."""
    row = pending_request(db, artifact_id, reviewer_id)
    if row is not None and row["state"] == "forked" and not fork_end:
        return ReviewTransition()
    return _submit_request(db, row, batch_id) if row is not None else ReviewTransition()


def settle_for_batch(db, batch) -> tuple[str, ...]:
    """Answer a pending request only from ordinary feedback delivered to its owner."""
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
    row = pending_request(db, batch["artifact_id"], batch["author_id"])
    if row is not None and row["state"] not in FALLBACK_STATES:
        return ()
    return _submit_request(db, row, batch["id"]).woken if row is not None else ()
