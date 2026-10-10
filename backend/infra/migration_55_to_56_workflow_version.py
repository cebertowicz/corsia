"""Schema 55 -> 56: rename plan_nodes.workflow_commit to workflow_version.

The column holds any workflow version identifier (a commit SHA, a dirty SHA,
or a system manifest version), not only a commit (#2876).
"""

from __future__ import annotations

import sqlite3


def migrate_55_to_56(conn: sqlite3.Connection) -> None:
    """Rename the workflow version column in place, keeping every stored value."""

    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(plan_nodes)")}
    if "workflow_commit" in columns and "workflow_version" not in columns:
        conn.execute(
            "ALTER TABLE plan_nodes RENAME COLUMN workflow_commit TO workflow_version"
        )
