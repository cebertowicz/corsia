"""Migration 33→34: bound newest-checkpoint lookups by thread."""

from __future__ import annotations

import sqlite3


def migrate_33_to_34(conn: sqlite3.Connection) -> None:
    """Add the index that serves a thread's newest checkpoint in order."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS checkpoints_listing "
        "ON checkpoints(thread_id,generation_order DESC)"
    )
