"""Feedback-batch transitions caused by a user's comment state change."""

from __future__ import annotations

from typing import Any

from doceditor import feedback_batches


def reopen_queues_feedback(comment: dict[str, Any]) -> bool:
    """Whether a user's re-open of this comment queues feedback for its author.

    Re-opening a rejected suggestion queues nothing: its author was told of the
    rejection by a passive note, and is told of the re-open the same way.
    """
    return not (comment.get("decision") == "rejected" and comment.get("decision_event_id"))


def apply_user_transition(
    db,
    comment: dict[str, Any] | None,
    patch: dict[str, Any],
    user: dict[str, Any],
    delivery_policy: str = feedback_batches.DOCUMENT_HELD,
) -> None:
    """Queue a user reopen in the person's lane; ``comments.update`` drops it if the comment is resolved again."""
    if not comment or not (comment.get("resolved") and patch.get("resolved") is False):
        return
    if not reopen_queues_feedback(comment):
        return
    feedback_batches.record_reopen(
        db, comment, user_id=user["id"], user_display=user["name"], delivery_policy=delivery_policy,
    )
