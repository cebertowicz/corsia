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


def turn_running_before(db: sqlite3.Connection, thread_id: str, moment: str) -> bool:
    """Whether a Turn that started before ``moment`` is still running."""
    return db.execute(
        "SELECT 1 FROM active_turns WHERE thread_id=? AND status='running' "
        "AND julianday(started_at) < julianday(?)",
        (thread_id, moment),
    ).fetchone() is not None


def liveness_wake_queued(db: sqlite3.Connection, thread_id: str) -> bool:
    """Whether a plan liveness wake in the thread's FIFO has not begun its write.

    Unclaimed and claimed-but-unwritten wakes both count: either will still be
    read. A claim held by a Turn that is no longer running does not, so an
    orphaned claim cannot suppress every later wake.
    """
    return db.execute(
        "SELECT 1 FROM messages m WHERE m.thread_id=? AND m.delivery_state='queued' "
        "AND m.input_order IS NOT NULL AND m.write_started_at IS NULL "
        "AND json_extract(m.metadata,'$.event_type')='plan.liveness_wake' "
        "AND (m.active_turn_id IS NULL OR EXISTS (SELECT 1 FROM active_turns t "
        "WHERE t.id=m.active_turn_id AND t.status='running')) LIMIT 1",
        (thread_id,),
    ).fetchone() is not None


def turn_starting_work_pending(db: sqlite3.Connection, thread_id: str) -> bool:
    """Whether a running Turn or current-model queued input already starts work."""
    if db.execute(
        "SELECT 1 FROM active_turns WHERE thread_id=? AND status='running'",
        (thread_id,),
    ).fetchone() is not None:
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
