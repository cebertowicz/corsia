"""Schema 77 -> 78: give existing databases the Measurement query indexes.

Purely additive: indexes only, no row or column changes. Runs inside the
runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

from infra.measurement_schema import MEASUREMENT_INDEXES


def migrate_77_to_78(conn: sqlite3.Connection) -> None:
    """Create each shared index a fresh schema has; a table the database lacks has nothing to index."""
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, sql in MEASUREMENT_INDEXES.items():
        if table in tables:
            conn.execute(sql)
