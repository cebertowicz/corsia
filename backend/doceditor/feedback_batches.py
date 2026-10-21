"""Durable batching and delivery for document comments and suggestions."""

from __future__ import annotations

import logging
from doceditor.feedback_batch_identity import open_batch
from doceditor.feedback_turn_closing import close_for_turn_end
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from doceditor import comment_notifications, feedback_comments, feedback_edits, review_feedback
from doceditor.feedback_batch_delivery import claim_and_deliver
from doceditor.feedback_state import (
    USER_AUTHOR_ID,
    annotate_pane,
    reply_waiting_ids,
    take_unreported_undelivered,
    undelivered_for_document,
    user_feedback_for_document,
)
from infra.database import write_transaction

log = logging.getLogger(__name__)

AGENT_QUIET = 120
USER_LEAVE_GRACE = 120
USER_IDLE = 30 * 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _after(value: str, seconds: int) -> str:
    return (datetime.fromisoformat(value) + timedelta(seconds=seconds)).isoformat()


def _new_deadline(author_kind: str, now: str) -> str:
    return _after(now, AGENT_QUIET if author_kind == "agent" else USER_IDLE)


def _touch_batch(
    db,
    batch_id: str,
    *,
    author_kind: str,
    now: str,
    item_delta: int,
) -> None:
    db.execute(
        """UPDATE doc_feedback_batches
           SET last_item_at=?,closes_at=?,closed_reason=NULL,
               item_count=item_count+?,updated_at=? WHERE id=?""",
        (now, _new_deadline(author_kind, now), item_delta, now, batch_id),
    )


def _add_comment_item(
    db,
    *,
    batch_id: str,
    comment_id: str,
    author_kind: str,
    now: str,
    event: str | None = None,
    event_by: str | None = None,
) -> bool:
    """Add one comment item once and keep the batch deadline/count in sync."""
    inserted = db.execute(
        """INSERT OR IGNORE INTO doc_feedback_batch_items
           (batch_id,comment_id,event,event_at,event_by) VALUES (?,?,?,?,?)""",
        (batch_id, comment_id, event, now if event else None, event_by),
    )
    if not inserted.rowcount:
        return False
    _touch_batch(db, batch_id, author_kind=author_kind, now=now, item_delta=1)
    return True


def _join_open_batch(
    db, comment: dict[str, Any], recipient_id: str, review_request_id: str | None = None,
) -> str:
    author_kind = str(comment["author_kind"])
    now = str(comment["created_at"])
    batch_id = open_batch(
        db,
        author_kind=author_kind,
        author_id=feedback_comments.author_id(comment),
        artifact_id=str(comment["artifact_id"]),
        recipient_id=recipient_id,
        now=now,
        closes_at=_new_deadline(author_kind, now),
        review_request_id=review_request_id,
    )
    _add_comment_item(
        db,
        batch_id=batch_id,
        comment_id=str(comment["id"]),
        author_kind=author_kind,
        now=now,
    )
    return batch_id


def record_reopen(
    db,
    comment: dict[str, Any],
    *,
    user_id: str,
    user_display: str,
    now: str | None = None,
) -> str | None:
    """Queue a user's resolved-comment re-open through canonical recipients."""
    if not user_id:
        return None
    at = now or _now()
    recipient_id, = feedback_comments.recipients(db, comment, document_owner_only=True)
    batch_id = open_batch(
        db,
        author_kind="user",
        author_id=USER_AUTHOR_ID,
        artifact_id=str(comment["artifact_id"]),
        recipient_id=recipient_id,
        now=at,
        closes_at=_new_deadline("user", at),
    )
    _add_comment_item(
        db,
        batch_id=batch_id,
        comment_id=str(comment["id"]),
        author_kind="user",
        now=at,
        event="reopened",
        event_by=user_display.strip() or user_id,
    )
    return batch_id


def record_edit(
    db,
    *,
    artifact_id: str,
    recipient_id: str,
    author_kind: str,
    author_id: str,
    actor_display: str,
    path: Path,
    before: bytes,
    after: bytes,
    at: str | None = None,
    prepared: dict | None = None,
) -> str:
    """Coalesce one substantive non-owner save into the canonical feedback batch."""
    if author_kind not in {"agent", "user"}:
        raise ValueError("edit feedback author_kind must be 'agent' or 'user'")
    now = at or _now()

    batch_id = open_batch(
        db,
        author_kind=author_kind,
        author_id=author_id,
        artifact_id=artifact_id,
        recipient_id=recipient_id,
        now=now,
        closes_at=_new_deadline(author_kind, now),
    )
    item_delta = feedback_edits.record_edit_item(
        db,
        batch_id=batch_id,
        actor_display=actor_display,
        path=path,
        before=before,
        after=after,
        now=now,
        prepared=prepared,
    )
    _touch_batch(db, batch_id, author_kind=author_kind, now=now, item_delta=item_delta)
    return batch_id


def record_comment(
    db,
    comment: dict[str, Any],
    *,
    created: bool,
    submit: bool = False,
    review_request_id: str | None = None,
) -> dict[str, Any]:
    """Attach a newly inserted comment to recipient batches in the caller's write."""
    batch_ids: list[str] = []
    woken: list[str] = []
    notices = (
        take_unreported_undelivered(db, feedback_comments.author_id(comment), _now())
        if comment["author_kind"] == "agent"
        else []
    )
    if created:
        feedback_comments.make_user_document_durable(db, comment)
        feedback_comments.emit_comment_created(db, comment)
        batch_ids = [
            _join_open_batch(db, comment, recipient, review_request_id)
            for recipient in feedback_comments.recipients(db, comment)
        ]
        if submit and comment["author_kind"] == "agent" and review_request_id is not None:
            pending, woken, failures = review_feedback.submit_fork_feedback(
                db, review_request_id, feedback_comments.author_id(comment), comment["artifact_id"],
            )
            return {"batch_ids": batch_ids, "woken": woken, "undelivered": failures}
        if submit and comment["author_kind"] == "agent":
            woken = close_for_document(
                db,
                author_kind="agent",
                author_id=feedback_comments.author_id(comment),
                artifact_id=comment["artifact_id"],
                reason="submit",
            )
    if submit and comment["author_kind"] == "agent":
        current_failures = undelivered_for_document(
            db,
            author_kind="agent",
            author_id=feedback_comments.author_id(comment),
            artifact_id=comment["artifact_id"],
        )
        known = {row["id"] for row in notices}
        notices.extend(row for row in current_failures if row["id"] not in known)
    return {"batch_ids": batch_ids, "undelivered_batches": notices, "woken": woken}


def close_batch(db, batch_id: str, reason: str) -> tuple[bool, tuple[str, ...]]:
    """Conditionally close and deliver one open batch exactly once."""
    with write_transaction(db, scope="close_doc_feedback_batch") as connection:
        return claim_and_deliver(connection, batch_id, from_state="open", reason=reason)


def retry_batch(db, batch_id: str) -> tuple[bool, tuple[str, ...]]:
    """Retry one undelivered batch without creating another node."""
    with write_transaction(db, scope="retry_doc_feedback_batch") as connection:
        return claim_and_deliver(connection, batch_id, from_state="undelivered", reason=None)


def close_for_document(
    db,
    *,
    author_kind: str,
    author_id: str,
    artifact_id: str,
    reason: str,
) -> list[str]:
    """Close all matching open recipient batches for one document."""
    rows = db.execute(
        """SELECT id FROM doc_feedback_batches WHERE state='open'
           AND author_kind=? AND author_id=? AND artifact_id=?
           AND (review_request_id IS NULL OR EXISTS (
               SELECT 1 FROM doc_review_requests r
               WHERE r.id=review_request_id AND r.state IN ('handed_back','failed')
           )) ORDER BY opened_at,id""",
        (author_kind, author_id, artifact_id),
    ).fetchall()
    woken: list[str] = []
    for row in rows:
        _closed, selected = close_batch(db, row["id"], reason)
        woken.extend(selected)
    return woken


def close_due(now: str, *, db=None) -> list[str]:
    """Deliver every due batch; one that raises rolls back alone, is logged, and retries next pass.

    Active review batches wait for submission or the continuing Turn. Ended
    continuations resolve their remaining undelivered comments without delivery.
    """
    deliveries: list[tuple[str, str]] = []
    with write_transaction(db, scope="close_due_doc_feedback") as connection:
        review_feedback.close_ended_continuations(connection)
        rows = connection.execute(
            """SELECT id,artifact_id,author_kind,closed_reason FROM doc_feedback_batches
               WHERE state='open' AND closes_at<=? AND (review_request_id IS NULL OR EXISTS (
                   SELECT 1 FROM doc_review_requests r WHERE r.id=review_request_id
                   AND r.state='submitted'
               ))
               ORDER BY closes_at,id""",
            (now,),
        ).fetchall()
        woken: list[str] = []
        for row in rows:
            reason = row["closed_reason"] or ("quiet" if row["author_kind"] == "agent" else "idle")
            try:
                closed, selected = close_batch(connection, row["id"], reason)
            except Exception:
                log.exception("document feedback batch %s failed to close", row["id"])
                continue
            woken.extend(selected)
            comment_notifications.collect_due_user_delivery(deliveries, connection, row, closed)
    comment_notifications.publish_due_user_feedback(deliveries)
    return woken


def mark_user_left(db, artifact_id: str) -> int:
    """Start the user's leave grace period for open batches on a document."""
    now = _now()
    changed = db.execute(
        """UPDATE doc_feedback_batches SET closes_at=?,closed_reason='leave',updated_at=?
           WHERE state='open' AND author_kind='user' AND author_id=? AND artifact_id=?""",
        (_after(now, USER_LEAVE_GRACE), now, USER_AUTHOR_ID, artifact_id),
    )
    return changed.rowcount


def mark_user_returned(db, artifact_id: str) -> int:
    """Cancel leave grace by restoring each open batch's idle deadline."""
    rows = db.execute(
        """SELECT id,last_item_at FROM doc_feedback_batches WHERE state='open'
           AND author_kind='user' AND author_id=? AND artifact_id=?""",
        (USER_AUTHOR_ID, artifact_id),
    ).fetchall()
    now = _now()
    for row in rows:
        db.execute(
            """UPDATE doc_feedback_batches SET closes_at=?,closed_reason=NULL,updated_at=?
               WHERE id=? AND state='open'""",
            (_after(row["last_item_at"], USER_IDLE), now, row["id"]),
        )
    return len(rows)


def retry_for_document(db, *, author_kind: str, author_id: str, artifact_id: str) -> list[str]:
    """Retry every undelivered recipient batch for one author's document."""
    rows = db.execute(
        """SELECT id FROM doc_feedback_batches WHERE state='undelivered'
           AND author_kind=? AND author_id=? AND artifact_id=? ORDER BY updated_at,id""",
        (author_kind, author_id, artifact_id),
    ).fetchall()
    woken: list[str] = []
    for row in rows:
        _retried, selected = retry_batch(db, row["id"])
        woken.extend(selected)
    return woken


# Read-only feedback projections are imported above to preserve existing callers.
