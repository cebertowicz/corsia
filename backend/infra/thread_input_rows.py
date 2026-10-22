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


def liveness_wake_read_in_turn_since(
    db: sqlite3.Connection, thread_id: str, since: str,
) -> bool:
    """Whether the thread's running Turn began writing a liveness wake since ``since``.

    Only the running Turn's own claimed inputs are read (the
    ``messages(active_turn_id, input_order)`` index), so the cost is bounded by
    one Turn's inputs, not the thread's history. A begun write stays a read
    reminder while it is queued, delivered or ambiguous: the in-flight, accepted
    and uncertain sequence-attempt states. A proved-no-write release clears
    ``write_started_at`` and the claim, so it cannot suppress the next wake.
    """
    return db.execute(
        "SELECT 1 FROM active_turns t CROSS JOIN messages m "
        "ON m.active_turn_id=t.id "
        "WHERE t.thread_id=? AND t.status='running' "
        "AND m.delivery_state IN ('queued','delivered','ambiguous') "
        "AND m.write_started_at IS NOT NULL "
        "AND julianday(m.write_started_at) > julianday(?) "
        "AND json_extract(m.metadata,'$.event_type')='plan.liveness_wake' LIMIT 1",
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
