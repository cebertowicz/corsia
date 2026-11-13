"""The copies a Retry sends for an ended input, and how to find the newest one."""

from __future__ import annotations

import sqlite3
import uuid

# The columns that say where an input's delivery stands, as chat reads them off its row.
DELIVERY_COLUMNS = ('delivery_state', 'delivery_error', 'write_outcome', 'active_turn_id')


def retry_id(message_id: str) -> str:
    """The id of the one copy a Retry of this input sends; a second press finds it instead of sending again."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"corsia:retry:{message_id}"))


def newest_attempt_delivery(db: sqlite3.Connection, thread_id: str, message_id: str, delivery: dict) -> dict:
    """The delivery columns of this input's newest Retry copy, all read off that one row; its own when it has none."""
    while True:
        row = db.execute(
            f"SELECT id,{','.join(DELIVERY_COLUMNS)} FROM messages WHERE id=? AND thread_id=?",
            (retry_id(message_id), thread_id),
        ).fetchone()
        if row is None:
            return delivery
        message_id, delivery = row[0], dict(zip(DELIVERY_COLUMNS, row[1:]))
