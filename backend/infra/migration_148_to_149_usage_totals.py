"""Schema 148 -> 149: totals of admitted install usage summaries, and the delivery ids already counted."""
from __future__ import annotations

import sqlite3

# Frozen at schema v149; a later change to the live table gets its own migration.
_V149_USAGE_TOTALS_SQL = """
    CREATE TABLE IF NOT EXISTS usage_install_totals (
        week TEXT NOT NULL,
        release_tag TEXT NOT NULL,
        os TEXT NOT NULL DEFAULT '',
        chip TEXT NOT NULL DEFAULT '',
        install_weeks INTEGER NOT NULL CHECK (install_weeks > 0),
        PRIMARY KEY (week, release_tag, os, chip)
    ) WITHOUT ROWID;

    CREATE TABLE IF NOT EXISTS usage_admitted_deliveries (
        delivery_id TEXT PRIMARY KEY,
        week TEXT NOT NULL
    ) WITHOUT ROWID;
"""


def migrate_148_to_149(conn: sqlite3.Connection) -> None:
    """Create the totals and delivery-id tables inside the runner's transaction; ``execute``, never ``executescript``."""
    for statement in _V149_USAGE_TOTALS_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)
