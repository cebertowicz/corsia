"""Schema 114 -> 115: index scheduled-update attempts."""

from __future__ import annotations

import sqlite3


def migrate_114_to_115(conn: sqlite3.Connection) -> None:
    """Index scheduled-update attempts without changing historical operations."""
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='self_restart_requests'"
    ).fetchone() is None:
        return
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_self_restart_requests_update_attempts "
        "ON self_restart_requests(kind, requested_at)"
    )
