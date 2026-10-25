"""Record comment reopens and document edits in existing feedback batches."""

from __future__ import annotations

from pathlib import Path

from doceditor import feedback_comments, feedback_edits
from doceditor.feedback_batch_core import (
    IMMEDIATE, add_comment_item, open_batch, person_comment_lane, touch_batch, utc_now,
)
from doceditor.feedback_state import USER_AUTHOR_ID


def record_reopen(
    db,
    comment: dict,
    *,
    user_id: str,
    user_display: str,
    delivery_policy: str,
    now: str | None = None,
) -> str | None:
    """Queue a user's resolved-comment re-open through canonical recipients, in the lane the person chose.

    A re-opened image-region comment is image-held whatever lane was named, as when it was created.
    """
    if not user_id:
        return None
    delivery_policy = person_comment_lane(delivery_policy, region=comment.get("region_x") is not None)
    at = now or utc_now()
    recipient_id, = feedback_comments.recipients(db, comment, document_owner_only=True)
    batch_id = open_batch(
        db,
        author_kind="user",
        author_id=USER_AUTHOR_ID,
        artifact_id=str(comment["artifact_id"]),
        recipient_id=recipient_id,
        delivery_policy=delivery_policy,
        now=at,
    )
    add_comment_item(
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
    """Coalesce one substantive non-owner save into the canonical feedback batch.

    A save goes in the immediate lane, a person's included (5 s idle, as before holds existed); the lane is a constant,
    not a caller choice. Suggestion accepts write through here and share it.
    """
    if author_kind not in {"agent", "user"}:
        raise ValueError("edit feedback author_kind must be 'agent' or 'user'")
    now = at or utc_now()
    batch_id = open_batch(
        db,
        author_kind=author_kind,
        author_id=author_id,
        artifact_id=artifact_id,
        recipient_id=recipient_id,
        delivery_policy=IMMEDIATE,
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
    touch_batch(
        db,
        batch_id,
        author_kind=author_kind,
        delivery_policy=IMMEDIATE,
        now=now,
        item_delta=item_delta,
    )
    return batch_id
