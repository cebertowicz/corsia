"""After-response announcement for a person's document comment."""

from __future__ import annotations

from doceditor import comment_notifications, feedback_batches


async def announce_person_comment_after_response(batch_ids: list[str], artifact_id: str, delivery_policy: str) -> None:
    """Announce a person's saved immediate comment that waits for no delivery.

    A comment with recipients stays in its open batch until 5 s pass with no further comment from
    the person, so a burst (each message under 5 s after the last) reaches the agent as one wake.
    The continuation sweep delivers it at that deadline, announces it then, and is the durable retry
    path; announcing here too would show it unsent. Held document and image comments remain in
    their policy-specific open batch. This is the only post-response seam allowed to announce a
    person comment.
    """
    if delivery_policy == feedback_batches.IMMEDIATE and not batch_ids:
        comment_notifications.publish_due_user_feedback([artifact_id])
