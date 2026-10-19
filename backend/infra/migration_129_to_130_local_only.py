"""Add durable local-only thread scope."""

from __future__ import annotations

import sqlite3


def migrate_129_to_130(conn: sqlite3.Connection) -> None:
    """Add the fields once, tolerating current-schema test fixtures rewound by version."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
    if "local_only" not in columns:
        conn.execute(
            "ALTER TABLE threads ADD COLUMN local_only INTEGER NOT NULL DEFAULT 0"
        )
    if "local_only_inherited_from_thread_id" not in columns:
        conn.execute(
            "ALTER TABLE threads ADD COLUMN local_only_inherited_from_thread_id TEXT "
            "REFERENCES threads(id)"
        )
