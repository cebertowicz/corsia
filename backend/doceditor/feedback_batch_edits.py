"""Record comment reopens and document edits in existing feedback batches."""

from __future__ import annotations

from pathlib import Path

from doceditor import feedback_batches, feedback_comments, feedback_edits


def record_reopen(
    db,
    comment: dict,
    *,
    user_id: str,
    user_display: str,
    delivery_policy: str = "document-held",
    now: str | None = None,
) -> str | None:
    """Queue a user's resolved-comment re-open through canonical recipients, in the lane the person chose."""
    if not user_id:
        return None
    at = now or feedback_batches._now()
    recipient_id, = feedback_comments.recipients(db, comment, document_owner_only=True)
    batch_id = feedback_batches._open_batch(
        db,
        author_kind="user",
        author_id=feedback_batches.USER_AUTHOR_ID,
        artifact_id=str(comment["artifact_id"]),
        recipient_id=recipient_id,
        delivery_policy=delivery_policy,
        now=at,
    )
    feedback_batches._add_comment_item(
        db,
        batch_id=batch_id,
        comment_id=str(comment["id"]),
        author_kind="user",
        delivery_policy=delivery_policy,
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
    now = at or feedback_batches._now()
    batch_id = feedback_batches._open_batch(
        db,
        author_kind=author_kind,
        author_id=author_id,
        artifact_id=artifact_id,
        recipient_id=recipient_id,
        delivery_policy=feedback_batches.DOCUMENT_HELD,
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
    feedback_batches._touch_batch(
        db,
        batch_id,
        author_kind=author_kind,
        delivery_policy=feedback_batches.DOCUMENT_HELD,
        now=now,
        item_delta=item_delta,
    )
    return batch_id
