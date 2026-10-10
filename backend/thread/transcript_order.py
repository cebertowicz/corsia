"""Single-owner primitives for durable transcript position and boundaries."""

from __future__ import annotations

import sqlite3


def draw_transcript_position(
    connection: sqlite3.Connection,
    thread_id: str,
) -> int:
    """Atomically spend and advance one session transcript position."""
    row = connection.execute(
        "UPDATE threads SET next_message_seq=next_message_seq + 1 "
        "WHERE id=? RETURNING next_message_seq - 1",
        (thread_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"message thread does not exist: {thread_id}")
    return int(row[0])


def destructive_boundary_version(
    connection: sqlite3.Connection,
    thread_id: str,
) -> tuple[int, int, int]:
    """Snapshot mutation epoch, Turn order, and accepted user-input order."""
    row = connection.execute(
        "SELECT s.history_epoch, "
        "(SELECT COALESCE(MAX(turn_order), 0) FROM active_turns "
        " WHERE thread_id = ?) AS turn_order, "
        "(SELECT COALESCE(MAX(input_order), 0) FROM messages "
        " WHERE thread_id = ? AND input_order IS NOT NULL) AS input_order "
        "FROM threads s WHERE s.id = ?",
        (thread_id, thread_id, thread_id),
    ).fetchone()
    if row is None:
        raise LookupError(f"thread does not exist: {thread_id}")
    return (int(row[0]), int(row[1]), int(row[2]))


def destructive_boundary_is_clear(
    db,
    thread_id: str,
    expected_version: tuple[int, int, int],
) -> bool:
    """Check the captured boundary and absence of an active owner in one txn."""
    if destructive_boundary_version(db, thread_id) != expected_version:
        return False
    return (
        db.execute(
            "SELECT 1 FROM active_turns "
            "WHERE thread_id = ? AND status='running' LIMIT 1",
            (thread_id,),
        ).fetchone()
        is None
    )
