"""Schema 64 -> 65: drop unused legacy session tables."""

from __future__ import annotations

import sqlite3


_TABLES = ("session_summaries", "activity_sessions")


def migrate_64_to_65(conn: sqlite3.Connection) -> None:
    """Drop unused legacy tables safely when upgrading an existing database."""
    for table in _TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
