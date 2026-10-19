"""Add per-project inference policy storage."""

from __future__ import annotations

import sqlite3


def migrate_129_to_130(conn: sqlite3.Connection) -> None:
    """Create project-local inference policy storage and durable local-only scope."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS project_inference_policies (
            project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            cloud_models_off INTEGER NOT NULL DEFAULT 0 CHECK (cloud_models_off IN (0,1))
        )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
    if "local_only" not in columns:
        conn.execute("ALTER TABLE threads ADD COLUMN local_only INTEGER NOT NULL DEFAULT 0")
    if "local_only_inherited_from_thread_id" not in columns:
        conn.execute(
            "ALTER TABLE threads ADD COLUMN local_only_inherited_from_thread_id TEXT "
            "REFERENCES threads(id)"
        )
