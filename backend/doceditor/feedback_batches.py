"""Durable batching and delivery for document comments and suggestions."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from doceditor import comment_notifications, feedback_comments, feedback_delivery, feedback_edits
from doceditor.review_lifecycle import settle_for_batch
from doceditor.feedback_state import (
    USER_AUTHOR_ID,
    annotate_pane,
    reply_waiting_ids,
    take_unreported_undelivered,
    undelivered_for_document,
    user_feedback_for_document,
)
import infra.sweep_wake as sweep_wake
from infra.database import claim_connection, write_transaction
from infra.write_effects import after_commit

log = logging.getLogger(__name__)

AGENT_QUIET = 120
USER_IDLE = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _after(value: str, seconds: int) -> str:
    return (datetime.fromisoformat(value) + timedelta(seconds=seconds)).isoformat()


def _new_deadline(author_kind: str, now: str) -> str:
    return _after(now, AGENT_QUIET if author_kind == "agent" else USER_IDLE)


def _open_batch(
    db,
    *,
    author_kind: str,
    author_id: str,
    artifact_id: str,
    recipient_id: str,
    now: str,
) -> str:
    """Return the canonical open batch for one author, artifact, and recipient."""
    row = db.execute(
        """SELECT id FROM doc_feedback_batches
           WHERE author_kind=? AND author_id=? AND artifact_id=?
             AND recipient_id=? AND state='open'""",
        (author_kind, author_id, artifact_id, recipient_id),
    ).fetchone()
    if row is not None:
        return str(row["id"])
    batch_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO doc_feedback_batches
           (id,artifact_id,author_kind,author_id,recipient_id,state,
            opened_at,last_item_at,closes_at,item_count,created_at,updated_at)
           VALUES (?,?,?,?,?,'open',?,?,?,0,?,?)""",
        (
            batch_id, artifact_id, author_kind, author_id, recipient_id,
            now, now, _new_deadline(author_kind, now), now, now,
        ),
    )
    # A new deadline can be earlier than the one the continuation loop is sleeping toward.
    after_commit(db, sweep_wake.sweep_soon)
    return batch_id


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


def _join_open_batch(db, comment: dict[str, Any], recipient_id: str) -> str:
    author_kind = str(comment["author_kind"])
    now = str(comment["created_at"])
    batch_id = _open_batch(
        db,
        author_kind=author_kind,
        author_id=feedback_comments.author_id(comment),
        artifact_id=str(comment["artifact_id"]),
        recipient_id=recipient_id,
        now=now,
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
    batch_id = _open_batch(
        db,
        author_kind="user",
        author_id=USER_AUTHOR_ID,
        artifact_id=str(comment["artifact_id"]),
        recipient_id=recipient_id,
        now=at,
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

    batch_id = _open_batch(
        db,
        author_kind=author_kind,
        author_id=author_id,
        artifact_id=artifact_id,
        recipient_id=recipient_id,
        now=now,
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
        batch_ids = [_join_open_batch(db, comment, recipient) for recipient in feedback_comments.recipients(db, comment)]
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


def deliver_person_comment(db, batch_ids: list[str], artifact_id: str) -> list[str]:
    """Deliver a person's just-saved comment now, each batch in its own write.

    The comment is already committed. A batch that fails here stays open with its
    deadline a few seconds out, so the continuation loop retries it. The comment's
    one change frame goes out here, after delivery, whatever its outcome.
    """
    woken: list[str] = []
    for batch_id in batch_ids:
        try:
            woken.extend(close_batch(db, batch_id, "send")[1])
        except Exception:
            log.exception("immediate delivery of document feedback batch %s failed; it stays open to retry", batch_id)
    comment_notifications.publish_due_user_feedback([artifact_id])
    return woken


def _claim_and_deliver(db, batch_id: str, *, from_state: str, reason: str | None, due_by: str | None = None) -> tuple[bool, tuple[str, ...]]:
    if (reason is not None) + (due_by is not None) != (from_state == "open"):
        raise ValueError("an open-batch claim names exactly one of a reason or a due cutoff; a retry names neither")
    now = _now()
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


def close_batch(db, batch_id: str, reason: str) -> tuple[bool, tuple[str, ...]]:
    """Conditionally close and deliver one open batch exactly once."""
    with write_transaction(db, scope="close_doc_feedback_batch") as connection:
        return _claim_and_deliver(connection, batch_id, from_state="open", reason=reason)


def _close_due_batch(db, batch_id: str, now: str) -> tuple[bool, tuple[str, ...]]:
    """The due sweep's claim: closes_at is re-checked in the claim and the reason derives from the row."""
    with write_transaction(db, scope="close_doc_feedback_batch") as connection:
        return _claim_and_deliver(connection, batch_id, from_state="open", reason=None, due_by=now)


def retry_batch(db, batch_id: str) -> tuple[bool, tuple[str, ...]]:
    """Retry one undelivered batch without creating another node."""
    with write_transaction(db, scope="retry_doc_feedback_batch") as connection:
        return _claim_and_deliver(connection, batch_id, from_state="undelivered", reason=None)


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
           AND author_kind=? AND author_id=? AND artifact_id=? ORDER BY opened_at,id""",
        (author_kind, author_id, artifact_id),
    ).fetchall()
    woken: list[str] = []
    for row in rows:
        _closed, selected = close_batch(db, row["id"], reason)
        woken.extend(selected)
    return woken


def close_for_turn_end(db, thread_id: str) -> list[str]:
    """Close an agent thread's open batches in the terminal write transaction."""
    rows = db.execute(
        """SELECT id FROM doc_feedback_batches
           WHERE state='open' AND author_kind='agent' AND author_id=?
           ORDER BY opened_at,id""",
        (thread_id,),
    ).fetchall()
    woken: list[str] = []
    for row in rows:
        _closed, selected = close_batch(db, row["id"], "turn_end")
        woken.extend(selected)
    return woken


def close_due(now: str, *, db=None) -> list[str]:
    """Deliver every due batch; one that raises rolls back alone, is logged, and retries next pass."""
    connection, owns_connection = claim_connection(db, "close_due_doc_feedback")
    try:
        rows = connection.execute(
            "SELECT id,artifact_id,author_kind FROM doc_feedback_batches "
            "WHERE state='open' AND closes_at<=? ORDER BY closes_at,id", (now,),
        ).fetchall()
        deliveries: list[tuple[str, str]] = []
        woken: list[str] = []
        for row in rows:
            try:
                closed, selected = _close_due_batch(connection, row["id"], now)
            except Exception:
                log.exception("document feedback batch %s failed to close", row["id"])
                continue
            woken.extend(selected)
            comment_notifications.collect_due_user_delivery(deliveries, connection, row, closed)
        comment_notifications.publish_due_user_feedback(deliveries)
        return woken
    finally:
        if owns_connection:
            connection.close()


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
