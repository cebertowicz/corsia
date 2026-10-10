"""Exact delegated-child Turn binding shared by completion-return consumers."""

from __future__ import annotations


def resolve_child_turn_for_parent(
    child_thread_id: str,
    parent_thread_id: str,
    *,
    expected_turn_id: str | None = None,
    db=None,
) -> str | None:
    """Return the child's running Turn iff it registers this exact parent."""
    from infra.database import get_db

    connection = db or get_db()
    try:
        exact_turn = " AND t.id = ?" if expected_turn_id is not None else ""
        values = (
            (child_thread_id, parent_thread_id, expected_turn_id)
            if expected_turn_id is not None
            else (child_thread_id, parent_thread_id)
        )
        row = connection.execute(
            "SELECT t.id AS turn_id FROM active_turns AS t "
            "JOIN threads AS s ON s.id = t.thread_id "
            "WHERE t.thread_id = ? AND t.status = 'running' "
            "AND COALESCE(s.parent_thread_id, s.spawned_by) = ?"
            + exact_turn
            + " ORDER BY t.turn_order DESC LIMIT 1",
            values,
        ).fetchone()
        return str(row["turn_id"]) if row is not None else None
    finally:
        if db is None:
            connection.close()
