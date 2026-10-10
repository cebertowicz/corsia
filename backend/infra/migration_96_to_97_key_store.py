"""Schema 96 -> 97: key store metadata and the key-write password count.

Purely additive: two new tables, no data to convert. Key values are never in
the database, so there is nothing to move here; an install keeps reading its
web search key from the settings value or the environment until the person
saves one in Settings › Keys.

The DDL is copied rather than imported from ``infra.key_store_schema`` so this
step keeps producing the shape it was written for.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

_KEY_STORE_97_DDL = (
    """
    CREATE TABLE IF NOT EXISTS key_store_entries (
        name TEXT PRIMARY KEY,
        label TEXT,
        description TEXT,
        added_by_thread_id TEXT,
        added_by_name TEXT,
        created_at TEXT NOT NULL,
        set_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS key_write_attempts (
        token_hash TEXT PRIMARY KEY
            REFERENCES auth_tokens(token_hash) ON DELETE CASCADE,
        failures INTEGER NOT NULL DEFAULT 0 CHECK (failures >= 0),
        updated_at TEXT NOT NULL
    )
    """,
)


def migrate_96_to_97(conn: sqlite3.Connection) -> None:
    """Create the key store's two tables."""
    for statement in _KEY_STORE_97_DDL:
        conn.execute(statement)
