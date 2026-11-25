"""Persist the reply a user saw in a document-comment thread."""

from __future__ import annotations

import json

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
            from plans.public import reconcile_person_comment_task

            reconcile_person_comment_task(connection, artifact_id)
            comment_notifications.announce_after_commit(connection, artifact_id)
        return remaining
    finally:
        db.close()


# Documents per write transaction, under SQLite's bound-parameter limit (three IN lists each).
_READ_CHUNK = 300


def mark_documents_read(user_id: str, documents: list[tuple[str, str]]) -> None:
    """Mark each document's unread items read up to the reply the person was shown.

    ``documents`` is (live artifact id, through reply id) pairs, already authorized. Every
    thread's stamp moves to its newest unread item at or before ``through``; an item that
    arrived after it stays unread. Suggestions are never decided here. Raises ValueError
    when a through reply is not on its document.
    """
    from plans.public import documents_with_open_comment_task, reconcile_person_comment_task

    db = get_db()
    try:
        for start in range(0, len(documents), _READ_CHUNK):
            chunk = dict(documents[start:start + _READ_CHUNK])
            with write_transaction(db, scope="mark_documents_read") as connection:
                marks = {}
                for row in connection.execute(
                    "SELECT id, artifact_id, created_at FROM doc_comments WHERE id IN (SELECT value FROM json_each(?))",
                    (json.dumps(list(chunk.values())),),
                ):
                    if chunk.get(row["artifact_id"]) == row["id"]:
                        marks[row["artifact_id"]] = (row["created_at"], row["id"])
                if len(marks) != len(chunk):
                    raise ValueError("reply_not_found")
                sql, params = feedback_state.unread_comment_rows(user_id, tuple(chunk))
                newest: dict[str, tuple[str, str]] = {}
                for row in connection.execute(
                    f"SELECT item.artifact_id, root.id AS root_id, item.created_at, item.id {sql}", params,
                ):
                    stamp = (row["created_at"], row["id"])
                    if stamp <= marks[row["artifact_id"]] and stamp > newest.get(row["root_id"], ("", "")):
                        newest[row["root_id"]] = stamp
                for root_id, (seen_at, item_id) in newest.items():
                    feedback_state.stamp_thread_view(connection, user_id, root_id, seen_at, item_id)
                for artifact_id in documents_with_open_comment_task(connection, chunk):
                    reconcile_person_comment_task(connection, artifact_id)
                for artifact_id in chunk:
                    comment_notifications.announce_after_commit(connection, artifact_id)
    finally:
        db.close()


def dismiss_all(user_id: str, artifact_id: str) -> None:
    """Clear the person's comment attention without deciding suggestions."""
    from doceditor import person_comment_attention
    from plans.public import reconcile_person_comment_task

    db = get_db()
    try:
        with write_transaction(db, scope="dismiss_doc_comment_attention") as connection:
            person_comment_attention.dismiss(connection, artifact_id, user_id)
            reconcile_person_comment_task(connection, artifact_id)
            comment_notifications.announce_after_commit(connection, artifact_id)
    finally:
        db.close()
