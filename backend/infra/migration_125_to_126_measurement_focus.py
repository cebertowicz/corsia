"""Schema 125 -> 126: Measurement-only focused attention records."""

from __future__ import annotations

import sqlite3


def migrate_125_to_126(conn: sqlite3.Connection) -> None:
    """Create focus records without changing shared presence tables."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS measurement_focus_users (
            user_id TEXT PRIMARY KEY,
            focused_since TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS measurement_focus_intervals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            surface TEXT NOT NULL CHECK (length(trim(surface)) > 0),
            thread_id TEXT NOT NULL REFERENCES threads(id),
            session_id TEXT NOT NULL,
            focused_at TEXT NOT NULL,
            blurred_at TEXT,
            CHECK (blurred_at IS NULL OR blurred_at >= focused_at)
        );
        CREATE INDEX IF NOT EXISTS idx_focus_intervals_thread_time
            ON measurement_focus_intervals(thread_id, focused_at);
        CREATE INDEX IF NOT EXISTS idx_focus_intervals_user_time
            ON measurement_focus_intervals(user_id, focused_at);
    """)
