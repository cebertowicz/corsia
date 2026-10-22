"""Read-only predicates over queued thread-input rows."""

from __future__ import annotations

import json
import sqlite3


def starts_turn(row: sqlite3.Row) -> bool:
    """Return false only for a passive context event waiting for activity."""
    if row["role"] != "system":
        return True
    metadata = json.loads(row["metadata"] or "{}")
    return not (
        isinstance(metadata, dict)
        and metadata.get("type") == "context_event"
        and metadata.get("event_disposition") == "passive"
    )


def turn_running(db: sqlite3.Connection, thread_id: str) -> bool:
    """Whether the thread has a running Turn."""
    return db.execute(
        "SELECT 1 FROM active_turns WHERE thread_id=? AND status='running'",
        (thread_id,),
    ).fetchone() is not None


def liveness_wake_queued(db: sqlite3.Connection, thread_id: str) -> bool:
    """Whether a plan liveness wake still waits, unclaimed, in the thread's FIFO."""
    return db.execute(
        "SELECT 1 FROM messages WHERE thread_id=? AND delivery_state='queued' "
        "AND active_turn_id IS NULL AND input_order IS NOT NULL "
        "AND json_extract(metadata,'$.event_type')='plan.liveness_wake' LIMIT 1",
        (thread_id,),
    ).fetchone() is not None


def liveness_wake_read_since(
    db: sqlite3.Connection, thread_id: str, since: str,
) -> bool:
    """Whether a liveness wake reached provider delivery since ``since``."""
    return db.execute(
        "SELECT 1 FROM messages WHERE thread_id=? AND delivery_state='queued' "
        "AND json_extract(metadata,'$.event_type')='plan.liveness_wake' "
        "AND write_started_at IS NOT NULL "
        "AND julianday(write_started_at) > julianday(?) LIMIT 1",
        (thread_id, since),
    ).fetchone() is not None


def turn_starting_work_pending(db: sqlite3.Connection, thread_id: str) -> bool:
    """Whether a running Turn or current-model queued input already starts work."""
    if turn_running(db, thread_id):
        return True
    thread = db.execute(
        "SELECT execution_epoch,model FROM threads WHERE id=?", (thread_id,),
    ).fetchone()
    if thread is None:
        return False
    rows = db.execute(
        "SELECT role,metadata FROM messages WHERE thread_id=? AND delivery_state='queued' "
        "AND active_turn_id IS NULL AND input_order IS NOT NULL "
        "AND target_execution_epoch=? AND target_model=?",
        (thread_id, thread["execution_epoch"], thread["model"]),
    ).fetchall()
    return any(starts_turn(row) for row in rows)
