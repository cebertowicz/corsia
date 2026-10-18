"""Schema 125 -> 126: focus spans on the presence visit rows, and their attention rollup.

Purely additive: three nullable columns, the empty rollup tables and the triggers
that keep it exact, and no backfill (the background refresh builds the rollup;
until it has, Measurement reads the spans). A visit row whose ``focus_spans`` is
NULL predates focus tracking and counts whole.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

from infra.presence_rollup_schema import PRESENCE_ROLLUP_STATEMENTS

_COLUMNS_126 = {
    "thread_presence": ("focus_spans", "focus_since"),
    "presence_visits": ("focus_spans",),
}


def migrate_125_to_126(conn: sqlite3.Connection) -> None:
    """Add the focus columns to a presence table that exists and lacks them, then the rollup."""
    for table, columns in _COLUMNS_126.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # an older database the ladder has not reached yet creates the table with the columns
        for column in columns:
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='presence_visits'").fetchone():
        for statement in PRESENCE_ROLLUP_STATEMENTS:
            conn.execute(statement)
