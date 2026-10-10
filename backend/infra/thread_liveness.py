"""Read the canonical Turn and input FIFO state for one thread."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from infra.database import get_db


@dataclass(frozen=True, slots=True)
class ThreadLiveness:
    """Current durable progress facts; no inferred health classification."""

    state: str
    unproductive_turns: int
    queued_inputs: int
    last_turn: dict | None


def _latest_terminal(connection: sqlite3.Connection, thread_id: str):
    return connection.execute(
        "SELECT id,status,ended_at,error FROM active_turns "
        "WHERE thread_id=? AND status IN ('completed','cancelled','error') "
        "ORDER BY turn_order DESC LIMIT 1",
        (thread_id,),
    ).fetchone()


def _project_turn(row) -> dict | None:
    if row is None:
        return None
    return {
        "id": str(row["id"]),
        "status": str(row["status"]),
        "ended_at": row["ended_at"],
        "error": row["error"],
    }


def read_thread_liveness(
    thread_id: str,
    *,
    turn_active: bool,
    db: sqlite3.Connection | None = None,
) -> ThreadLiveness:
    """Return running/idle plus the visible queued count and latest terminal."""
    connection = db or get_db()
    try:
        queued = int(connection.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id=? "
            "AND input_order IS NOT NULL AND delivery_state='queued'",
            (thread_id,),
        ).fetchone()[0])
        latest = _latest_terminal(connection, thread_id)
    finally:
        if db is None:
            connection.close()
    return ThreadLiveness(
        state="running" if turn_active else "idle",
        unproductive_turns=0,
        queued_inputs=queued,
        last_turn=_project_turn(latest),
    )
