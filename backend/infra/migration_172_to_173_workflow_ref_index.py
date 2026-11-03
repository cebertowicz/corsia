"""Schema 172 -> 173: an index on the workflow a run phase was started from.

The acceptance-gate handoff finds a gate's runs by workflow and step inside the person's answer, so that read
must not scan ``plan_nodes``. The index is partial: only run phases carry a ``workflow_ref``.

Idempotent: ``IF NOT EXISTS``. Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3


def migrate_172_to_173(conn: sqlite3.Connection) -> None:
    """Create the partial ``workflow_ref`` index on ``plan_nodes``, once the column exists.

    Every real database has it since 52, but the older rungs' migration tests run this on a
    partial schema with no ``plan_nodes`` table, so the guard stays.
    """
    if "workflow_ref" not in {str(row[1]) for row in conn.execute("PRAGMA table_info(plan_nodes)")}:
        return
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_plan_nodes_workflow_ref ON plan_nodes(workflow_ref)"
        " WHERE workflow_ref IS NOT NULL"
    )
