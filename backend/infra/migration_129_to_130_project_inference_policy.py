"""Add per-project inference policy storage."""

from __future__ import annotations

import sqlite3


def migrate_129_to_130(conn: sqlite3.Connection) -> None:
    """Create the project-local cloud-model policy table."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS project_inference_policies (
            project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            cloud_models_off INTEGER NOT NULL DEFAULT 0 CHECK (cloud_models_off IN (0,1))
        )"""
    )
