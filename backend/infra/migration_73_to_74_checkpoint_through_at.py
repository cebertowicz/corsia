"""Schema 73 -> 74: the time a checkpoint's covered history ends at.

#3196: a checkpoint written long after a thread's last activity read as fresh
instruction, because nothing in the rendered checkpoint said when its history
ended. The column is additive and nullable: rows installed before this change
keep NULL here (74 -> 75 backfills them from their boundary message), and
every install after this change writes it
exactly once, inside the install transaction, from the boundary message's own
creation time.

The step is idempotent, like every other column-adding step here: a ladder test
that builds its old database from the current schema already carries the column,
so it is added only when it is actually missing, and no test has to rewind it.
A database without the table has nothing to add to, as in 65 -> 66.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3


def migrate_73_to_74(conn: sqlite3.Connection) -> None:
    """Add ``through_at`` to a database that has the table and lacks the column."""
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "checkpoints" not in tables:
        return
    present = {str(row[1]) for row in conn.execute("PRAGMA table_info(checkpoints)")}
    if "through_at" not in present:
        conn.execute("ALTER TABLE checkpoints ADD COLUMN through_at TEXT")