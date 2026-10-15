"""Schema 116 -> 117: persist lesson placement-check observations."""
from __future__ import annotations

import sqlite3


def migrate_116_to_117(conn: sqlite3.Connection) -> None:
    """Create the idempotent check-observation store without changing lesson files."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS lesson_placement_checks (
            thread_id TEXT NOT NULL REFERENCES threads(id),
            history_epoch INTEGER NOT NULL,
            entry_id INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            check_name TEXT NOT NULL,
            flag TEXT NOT NULL,
            override_reason TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (thread_id, history_epoch, entry_id, revision, check_name, flag)
        );
        CREATE INDEX IF NOT EXISTS idx_lesson_placement_checks_thread
            ON lesson_placement_checks(thread_id, check_name);
    """)
