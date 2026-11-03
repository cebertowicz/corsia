"""Schema 170 -> 171: an index on the workflow a run phase was started from.

The acceptance-gate handoff finds a gate's runs by workflow and step inside the person's answer, so that read
must not scan ``plan_nodes``. The index is partial: only run phases carry a ``workflow_ref``.

Idempotent: ``IF NOT EXISTS``. Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3


def migrate_170_to_171(conn: sqlite3.Connection) -> None:
    """Create the partial ``workflow_ref`` index on ``plan_nodes``; the column is there since 52."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_plan_nodes_workflow_ref ON plan_nodes(workflow_ref)"
        " WHERE workflow_ref IS NOT NULL"
    )
