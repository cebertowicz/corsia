"""Schema 114 -> 115: durable scheduled-update callback and outcome state."""

from __future__ import annotations

import sqlite3


_COLUMNS = (
    ("callback_credential_hash", "TEXT"),
    ("overnight_outcome_claimed_at", "TEXT"),
)


def migrate_114_to_115(conn: sqlite3.Connection) -> None:
    """Add scheduled-update fields without changing historical operations."""
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_self_restart_requests_update_attempts "
        "ON self_restart_requests(kind, requested_at)"
    )
