"""Persist the reply a user saw in a document-comment thread."""

from __future__ import annotations

from doceditor import comment_notifications, feedback_state
from infra.database import get_db, write_transaction


def mark_viewed(user_id: str, artifact_id: str, root_id: str, seen_reply_id: str) -> list[str] | None:
    """Advance one thread view stamp and announce the committed projection change.

    Returns the thread's new reply ids that remain after the stamp. ``None``
    distinguishes an invalid root/reply pair from a valid stamp.
    """
    db = get_db()
    try:
        with write_transaction(db, scope="mark_doc_comment_thread_viewed") as connection:
            try:
                remaining = feedback_state.mark_thread_viewed(
                    connection, user_id, artifact_id, root_id, seen_reply_id,
                )
            except ValueError:
                return None
            comment_notifications.announce_after_commit(connection, artifact_id)
        return remaining
    finally:
        db.close()
