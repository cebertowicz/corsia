"""Schema 42 -> 43: frozen artifact and plan-status snapshot columns.

#1602: an installed checkpoint stores the thread's owned and followed visible
artifacts and its bounded plan status, both frozen at install, so the first
turn after a fold knows the thread's files and plan without a tool call. The
two columns are additive and nullable: existing rows keep NULL (nothing was
captured for them, so their rendered prefix is unchanged), and every install
after this change writes both columns exactly once inside the install
transaction.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3


def migrate_42_to_43(conn: sqlite3.Connection) -> None:
    """Add ``artifact_snapshot_json`` and ``plan_status_snapshot_json``."""
    conn.execute("ALTER TABLE checkpoints ADD COLUMN artifact_snapshot_json TEXT")
    conn.execute("ALTER TABLE checkpoints ADD COLUMN plan_status_snapshot_json TEXT")
