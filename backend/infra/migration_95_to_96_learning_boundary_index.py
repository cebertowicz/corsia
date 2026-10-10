"""Schema 95 -> 96: give existing databases the installed-boundary index.

Purely additive: one index, no row or column changes. Building it reads the
messages table once, so the step's time grows with the database file.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

from infra.learning_schema import LEARNING_INDEXES


def migrate_95_to_96(conn: sqlite3.Connection) -> None:
    """Create each shared index a fresh schema has; a table the database lacks has nothing to index."""
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, sql in LEARNING_INDEXES.items():
        if table in tables:
            conn.execute(sql)
