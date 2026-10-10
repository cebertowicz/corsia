"""Schema 50 -> 51: give agent-roster writes their own revision.

Generic thread writes update ``threads.updated_at`` but must not invalidate a
conductor's untouched roster entry. Existing rows inherit their current thread
revision; new writes advance only this roster revision.

Runs inside the migration transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3


def migrate_50_to_51(conn: sqlite3.Connection) -> None:
    """Add and backfill the agent-roster concurrency revision exactly once."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
    if "agent_roster_updated_at" not in columns:
        conn.execute(
            "ALTER TABLE threads ADD COLUMN agent_roster_updated_at TEXT "
            "NOT NULL DEFAULT '1970-01-01T00:00:00+00:00'"
        )
    conn.execute(
        "UPDATE threads SET agent_roster_updated_at=updated_at "
        "WHERE agent_roster_updated_at='1970-01-01T00:00:00+00:00'"
    )
