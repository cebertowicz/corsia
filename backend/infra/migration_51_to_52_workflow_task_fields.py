"""Add immutable workflow-attribution fields to plan nodes."""

from __future__ import annotations

import sqlite3


def migrate_51_to_52(conn: sqlite3.Connection) -> None:
    """Add nullable workflow task fields without rewriting existing plan rows."""

    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(plan_nodes)")}
    additions = (
        ("workflow_ref", "TEXT"),
        ("workflow_commit", "TEXT"),
        ("workflow_step", "TEXT"),
        (
            "difficulty",
            "TEXT CHECK (difficulty IS NULL OR difficulty IN "
            "('routine', 'standard', 'hard'))",
        ),
    )
    for name, definition in additions:
        if name not in columns:
            conn.execute(f"ALTER TABLE plan_nodes ADD COLUMN {name} {definition}")
