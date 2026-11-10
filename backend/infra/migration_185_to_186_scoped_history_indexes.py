"""Schema 185 -> 186: give existing databases the scoped-history indexes.

Purely additive: indexes only, no row or column changes. Building them reads the
messages table once each, so the step's time grows with the database file
(measured 4.2 s on a synthetic 3,053,475-row database, warm page cache).

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

from infra.scoped_history_indexes import SCOPED_HISTORY_INDEXES


def migrate_185_to_186(conn: sqlite3.Connection) -> None:
    """Create each scoped-history index a fresh schema has; a table the database lacks has nothing to index."""
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, sql in SCOPED_HISTORY_INDEXES:
        if table in tables:
            conn.execute(sql)
