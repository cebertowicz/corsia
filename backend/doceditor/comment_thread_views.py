"""Persist the reply a user saw in a document-comment thread."""

from __future__ import annotations

from collections.abc import Sequence

from doceditor import comment_notifications, feedback_state
from infra.database import get_db, write_transaction


def mark_viewed(
    user_id: str, lineage: str | Sequence[str], root_id: str, seen_reply_id: str,
) -> list[str] | None:
    """Advance one thread view stamp and announce the committed projection change.

    ``lineage`` is one document or the identities of one, live first; the thread may be on any of them.

    Returns the thread's new reply ids that remain after the stamp. ``None``
    distinguishes an invalid root/reply pair from a valid stamp.
    """
    db = get_db()
    try:
        with write_transaction(db, scope="mark_doc_comment_thread_viewed") as connection:
            try:
                remaining = feedback_state.mark_thread_viewed(
                    connection, user_id, lineage, root_id, seen_reply_id,
                )
            except ValueError:
                return None
            from plans.public import reconcile_person_comment_task

            for artifact_id in feedback_state.artifact_scope(lineage):
                reconcile_person_comment_task(connection, artifact_id)
                comment_notifications.announce_after_commit(connection, artifact_id)
        return remaining
    finally:
        db.close()


def dismiss_all(user_id: str, lineage: str | Sequence[str]) -> None:
    """Clear the person's comment attention across the document's lineage without deciding suggestions."""
    from doceditor import person_comment_attention
    from plans.public import reconcile_person_comment_task

    db = get_db()
    try:
        with write_transaction(db, scope="dismiss_doc_comment_attention") as connection:
            person_comment_attention.dismiss(connection, lineage, user_id)
            for artifact_id in feedback_state.artifact_scope(lineage):
                reconcile_person_comment_task(connection, artifact_id)
                comment_notifications.announce_after_commit(connection, artifact_id)
    finally:
        db.close()
