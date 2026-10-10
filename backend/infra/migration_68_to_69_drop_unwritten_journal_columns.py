"""Schema 68 -> 69: drop the four never-written ``context_change_journal`` columns.

``old_hash``, ``new_hash``, ``diff`` and ``reason`` had no producer: every
``append_change`` caller omitted them, so every row holds NULL in each. The
journal keeps ``summary`` and ``details_json`` for what changed.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

_COLUMNS = ("old_hash", "new_hash", "diff", "reason")


def migrate_68_to_69(conn: sqlite3.Connection) -> None:
    """Drop each unwritten journal column that is still present."""
    present = {row[1] for row in conn.execute("PRAGMA table_info(context_change_journal)")}
    for column in _COLUMNS:
        if column in present:
            conn.execute(f"ALTER TABLE context_change_journal DROP COLUMN {column}")
