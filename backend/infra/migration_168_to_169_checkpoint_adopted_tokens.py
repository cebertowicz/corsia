"""Schema 168 -> 169: the size a checkpoint's continuation measured at first adoption.

An optional fold runs beside the live Turn, so the continuation it installs can
land at or above the fold launch point once the Turn's tail is counted. The size
is known only when the Turn first renders the installed continuation, so that
render writes it once. The column is additive and nullable: rows from before
this change, and checkpoints never adopted, keep NULL (not known to be over);
nothing is backfilled.

Idempotent like 73 -> 74: a ladder test that builds its old database from the
current schema already carries the column. A database without the table has
nothing to add to.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3


def migrate_168_to_169(conn: sqlite3.Connection) -> None:
    """Add ``adopted_input_tokens`` to a database that has the table and lacks the column."""
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "checkpoints" not in tables:
        return
    present = {str(row[1]) for row in conn.execute("PRAGMA table_info(checkpoints)")}
    if "adopted_input_tokens" not in present:
        conn.execute("ALTER TABLE checkpoints ADD COLUMN adopted_input_tokens INTEGER")
