"""Schema 147 -> 148: totals of admitted install usage summaries."""
from __future__ import annotations

import sqlite3

# Frozen at schema v148; a later change to the live table gets its own migration.
_V148_USAGE_TOTALS_SQL = """
    CREATE TABLE IF NOT EXISTS usage_install_totals (
        week TEXT NOT NULL,
        release_tag TEXT NOT NULL,
        os TEXT NOT NULL DEFAULT '',
        chip TEXT NOT NULL DEFAULT '',
        install_weeks INTEGER NOT NULL CHECK (install_weeks > 0),
        PRIMARY KEY (week, release_tag, os, chip)
    ) WITHOUT ROWID;
"""


def migrate_147_to_148(conn: sqlite3.Connection) -> None:
    """Create the totals table inside the runner's transaction; ``execute``, never ``executescript``."""
    for statement in _V148_USAGE_TOTALS_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)
