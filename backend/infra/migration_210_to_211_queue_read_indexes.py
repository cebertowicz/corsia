"""Schema 210 -> 211: the two indexes Activity's queue read needs.

Each poll of the person's queue finds the plans holding an open task assigned to
them, and the identities whose metadata marks them raised. Without these indexes
the first read steps through every plan node the person's threads own and the
second parses every active identity's metadata, so a poll's cost grows with all
history rather than with what waits on the person.

Adds the indexes only; no row changes, and a second run does nothing. Building
them reads each table once. Runs inside the runner's transaction; it never commits.
"""

from __future__ import annotations

import sqlite3

from infra.artifact_identity_schema import ATTENTION_STATE_INDEX_SQL
from infra.plan_schema import USER_OPEN_TASK_INDEX_SQL


def migrate_210_to_211(conn: sqlite3.Connection) -> None:
    """Create each index whose table the database has."""
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, sql in (("plan_nodes", USER_OPEN_TASK_INDEX_SQL), ("artifact_identities", ATTENTION_STATE_INDEX_SQL)):
        if table in tables:
            conn.execute(sql.strip().rstrip(";"))
