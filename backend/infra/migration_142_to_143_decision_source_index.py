"""Schema 142 -> 143: index each decision by the task it was raised on.

A task's waiting time leaves out the stretches a decision on it waited for the
user's answer (``plans.node_waiting``), so a task read looks its decisions up
by source task. Without this index that lookup scans every decision ever
raised. Adds the index only; no row changes, and a second run does nothing.
Runs inside the runner's transaction; it never commits.
"""

from __future__ import annotations

import sqlite3

from infra.artifact_identity_schema import DECISION_SOURCE_INDEX_SQL


def migrate_142_to_143(conn: sqlite3.Connection) -> None:
    """Create the decision source-task index on an existing identity table."""
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifact_identities'"
    ).fetchone() is None:
        return
    conn.execute(DECISION_SOURCE_INDEX_SQL.strip().rstrip(";"))
