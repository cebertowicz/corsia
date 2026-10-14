"""Schema 113 -> 114: persist update-operation identity and terminal causes."""

from __future__ import annotations

import sqlite3


_COLUMNS = (
    ("kind", "TEXT"),
    ("trigger", "TEXT"),
    ("target_version", "TEXT"),
    ("drain_total", "INTEGER"),
    ("failure_code", "TEXT"),
    ("failure_detail", "TEXT"),
    ("conductor_told_at", "TEXT"),
    ("changes", "TEXT"),
    ("outcome_seen_at", "TEXT"),
)


def migrate_113_to_114(conn: sqlite3.Connection) -> None:
    """Add durable Update fields without changing historical operation rows."""
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='self_restart_requests'"
    ).fetchone() is None:
        return
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(self_restart_requests)")
    }
    for name, definition in _COLUMNS:
        if name not in columns:
            conn.execute(
                f"ALTER TABLE self_restart_requests ADD COLUMN {name} {definition}"
            )
