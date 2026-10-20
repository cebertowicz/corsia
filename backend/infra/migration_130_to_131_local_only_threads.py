"""Schema 130 -> 131: durable Local only scope on threads."""

from __future__ import annotations

import sqlite3


def migrate_130_to_131(conn: sqlite3.Connection) -> None:
    """Add each thread's own effective Local only value; existing threads start off."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
    if columns and "local_only" not in columns:
        conn.execute("ALTER TABLE threads ADD COLUMN local_only INTEGER NOT NULL DEFAULT 0")
