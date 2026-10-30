"""Durable batching and delivery for document comments and suggestions."""

from __future__ import annotations

import logging
from typing import Any

from doceditor import comment_notifications, feedback_comments
from doceditor.feedback_batch_core import (
    AGENT_QUIET,
    DOCUMENT_HELD,
    HELD_POLICIES,
    IMAGE_HELD,
    IMMEDIATE,
    IMMEDIATE_IDLE,
    USER_IDLE,
    add_comment_item,
    new_deadline,
    open_batch,
    utc_now,
)
from doceditor.feedback_batch_delivery import claim_and_deliver
from doceditor.feedback_state import (
    USER_AUTHOR_ID,
    annotate_pane,
    reply_waiting_ids,
    RETRY_LANES,
    held_counts_for_thread,
    open_counts_for_document,
    take_unreported_undelivered,
    undelivered_for_document,
    user_feedback_for_document,
)
from infra.database import claim_connection, write_transaction

log = logging.getLogger(__name__)


def _join_open_batch(db, comment: dict[str, Any], recipient_id: str, delivery_policy: str) -> str:
    author_kind = str(comment["author_kind"])
    now = str(comment["created_at"])
    batch_id = open_batch(
        db,
        author_kind=author_kind,
        author_id=feedback_comments.author_id(comment),
        artifact_id=str(comment["artifact_id"]),
        recipient_id=recipient_id,
        delivery_policy=delivery_policy,
        now=now,
    )
    add_comment_item(
        db,
        batch_id=batch_id,
        comment_id=str(comment["id"]),
        author_kind=author_kind,
        delivery_policy=delivery_policy,
        now=now,
    )
    return batch_id



def record_comment(
    db,
    comment: dict[str, Any],
    *,
    created: bool,
    submit: bool = False,
    delivery_policy: str = IMMEDIATE,
) -> dict[str, Any]:
    """Attach a newly inserted comment to recipient batches in the caller's write."""
    batch_ids: list[str] = []
    woken: list[str] = []
    notices = (
        take_unreported_undelivered(db, feedback_comments.author_id(comment), utc_now())
        if comment["author_kind"] == "agent"
        else []
    )
    if created:
        feedback_comments.make_user_document_durable(db, comment)
        feedback_comments.emit_comment_created(db, comment)
        batch_ids = [
            _join_open_batch(db, comment, recipient, delivery_policy)
            for recipient in feedback_comments.recipients(db, comment)
        ]
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
    return claim_and_deliver(
        db, batch_id, from_state=from_state, reason=reason, due_by=due_by, now=utc_now(),
    )


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
    delivery_policy: str | None = None,
) -> list[str]:
    """Close all matching open recipient batches for one document and optional lane."""
    policy_sql = " AND delivery_policy=?" if delivery_policy else ""
    params: tuple[str, ...] = (author_kind, author_id, artifact_id)
    if delivery_policy:
        params += (delivery_policy,)
    rows = db.execute(
        """SELECT id FROM doc_feedback_batches WHERE state='open'
           AND author_kind=? AND author_id=? AND artifact_id=?""" + policy_sql + " ORDER BY opened_at,id",
        params,
    ).fetchall()
    woken: list[str] = []
    for row in rows:
        _closed, selected = close_batch(db, row["id"], reason)
        woken.extend(selected)
    return woken


def touch_for_document(db, *, artifact_id: str, delivery_policy: str) -> int:
    """Extend one held document or image lane without allowing a policy mix."""
    if delivery_policy not in HELD_POLICIES:
        raise ValueError("only held feedback lanes may be touched")
    now = utc_now()
    changed = db.execute(
        """UPDATE doc_feedback_batches
           SET last_item_at=?, closes_at=?, updated_at=?
           WHERE state='open' AND author_kind='user' AND author_id=?
             AND artifact_id=? AND delivery_policy=?""",
        (now, new_deadline("user", delivery_policy, now), now, USER_AUTHOR_ID, artifact_id, delivery_policy),
    )
    return changed.rowcount


def held_for_thread(db, thread_id: str) -> dict[str, Any]:
    """Project the document-held person comments whose recipient is the current thread.

    Image-held work belongs to its image viewer and never appears here. One row per
    held document, so the result is bounded by open documents, not comments.
    """
    artifacts = held_counts_for_thread(db, thread_id, DOCUMENT_HELD)
    return {"open_count": sum(item["open_count"] for item in artifacts), "artifacts": artifacts}


def close_for_thread(db, *, thread_id: str, reason: str) -> list[str]:
    """Deliver every document-held person-comment lane routed to the current thread.

    Each batch closes conditionally on being open, so a document sender that won
    first leaves nothing here and the same comments are never delivered twice.
    """
    rows = db.execute(
        """SELECT id FROM doc_feedback_batches
           WHERE state='open' AND author_kind='user' AND author_id=? AND recipient_id=?
             AND delivery_policy=? ORDER BY opened_at,id""",
        (USER_AUTHOR_ID, thread_id, DOCUMENT_HELD),
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


def retry_for_document(
    db, *, author_kind: str, author_id: str, artifact_id: str, delivery_policy: str,
) -> list[str]:
    """Retry one lane's undelivered recipient batches for one document, with what failed beside it (RETRY_LANES)."""
    policies = RETRY_LANES[delivery_policy]
    rows = db.execute(
        f"""SELECT id FROM doc_feedback_batches WHERE state='undelivered'
            AND author_kind=? AND author_id=? AND artifact_id=?
            AND delivery_policy IN ({','.join('?' * len(policies))})
            ORDER BY updated_at,id""",
        (author_kind, author_id, artifact_id, *policies),
    ).fetchall()
    woken: list[str] = []
    for row in rows:
        _retried, selected = retry_batch(db, row["id"])
        woken.extend(selected)
    return woken
