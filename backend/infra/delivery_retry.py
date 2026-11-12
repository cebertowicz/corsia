"""The copies a Retry sends for an ended input, and how to find the newest one."""

from __future__ import annotations

import sqlite3
import uuid


def retry_id(message_id: str) -> str:
    """The id of the one copy a Retry of this input sends; a second press finds it instead of sending again."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"corsia:retry:{message_id}"))


def newest_attempt_delivery(db: sqlite3.Connection, thread_id: str, message_id: str,
                            delivery_state: str | None, delivery_error: str | None) -> tuple[str | None, str | None]:
    """The delivery state and error of this input's newest Retry copy, or its own when it has none."""
    while True:
        row = db.execute(
            "SELECT id,delivery_state,delivery_error FROM messages WHERE id=? AND thread_id=?",
            (retry_id(message_id), thread_id),
        ).fetchone()
        if row is None:
            return delivery_state, delivery_error
        message_id, delivery_state, delivery_error = row
