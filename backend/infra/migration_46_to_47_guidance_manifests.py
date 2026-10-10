"""Schema 46 -> 47: which guidance, at which version, each Turn delivered.

Adds the content-addressed guidance list table, the per-read ``SKILL.md``
receipt table, a list id on each usage row, and the list id plus recording
statuses on each Turn. Every new column is nullable: rows written before this
step have no list, and saying so is the truthful answer. New Turns start
``unbound``, so a Turn whose binding never landed is not mistaken for one of
those older rows.

The DDL is copied rather than imported from ``infra.usage_schema``
so this step keeps producing the shape it was written for even after that
module's DDL changes later.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

_GUIDANCE_47_DDL = (
    """
    CREATE TABLE IF NOT EXISTS context_manifests (
        id TEXT PRIMARY KEY,
        harness TEXT NOT NULL,
        selector TEXT,
        resolved_model TEXT,
        resolved_model_source TEXT,
        entries_json TEXT NOT NULL CHECK (json_valid(entries_json)),
        entry_count INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS guidance_loads (
        id TEXT PRIMARY KEY,
        thread_id TEXT,
        turn_id TEXT,
        path TEXT NOT NULL,
        raw_sha256 TEXT NOT NULL,
        git_blob_oid TEXT NOT NULL,
        delivered_sha256 TEXT NOT NULL,
        byte_count INTEGER NOT NULL,
        partial INTEGER NOT NULL CHECK (partial IN (0, 1)),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_guidance_loads_turn
        ON guidance_loads(turn_id) WHERE turn_id IS NOT NULL
    """,
)

_COLUMNS_47 = (
    ("llm_usage", "context_manifest_id", "TEXT"),
    ("active_turns", "context_manifest_id", "TEXT"),
    ("active_turns", "guidance_load_status", "TEXT"),
    ("active_turns", "context_manifest_status", "TEXT DEFAULT 'unbound'"),
)


def migrate_46_to_47(conn: sqlite3.Connection) -> None:
    """Create the guidance tables and add the list id columns."""
    for statement in _GUIDANCE_47_DDL:
        conn.execute(statement)
    for table, column, kind in _COLUMNS_47:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            if "DEFAULT" in kind:
                # The default is for Turns created from now on, not older rows.
                conn.execute(f"UPDATE {table} SET {column}=NULL")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_usage_context_manifest"
        " ON llm_usage(context_manifest_id) WHERE context_manifest_id IS NOT NULL"
    )
