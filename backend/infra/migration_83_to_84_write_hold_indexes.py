"""Schema 83 -> 84: give existing databases the write-hold indexes.

Purely additive: indexes only, no row or column changes. Building them reads
the messages table once, so the step's time grows with the database file.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

from infra.write_hold_indexes import WRITE_HOLD_INDEXES


def migrate_83_to_84(conn: sqlite3.Connection) -> None:
    """Create each write-hold index a fresh schema has; a table the database lacks has nothing to index."""
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, sql in WRITE_HOLD_INDEXES:
        if table in tables:
            conn.execute(sql)
