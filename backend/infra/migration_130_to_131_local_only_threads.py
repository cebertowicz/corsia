"""Schema 130 -> 131: durable Local only scope on threads."""

from __future__ import annotations

import sqlite3


def migrate_130_to_131(conn: sqlite3.Connection) -> None:
    """Add the Local only root-owned scope to threads."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
    if not columns:
        return
    if "local_only" not in columns:
        conn.execute("ALTER TABLE threads ADD COLUMN local_only INTEGER NOT NULL DEFAULT 0")
    if "local_only_root_thread_id" not in columns:
        conn.execute(
            "ALTER TABLE threads ADD COLUMN local_only_root_thread_id TEXT "
            "REFERENCES threads(id)"
        )
