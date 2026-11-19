"""Schema 201 -> 202: the fresh-read record, passed bytes and document views.

Creates the three tables ``core.fresh_read`` writes and their indexes, plus one
partial index on ``messages`` holding only the person's own messages (built by one
pass over the table; skipped, as earlier steps do, on a database with no
``messages`` table). Nothing existing changes, and no row is written: an install
starts with no passes, so nothing it already shows the person is held back by this
step. The step runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

from infra.fresh_read_schema import FRESH_READ_SCHEMA_SQL, PERSON_INDEX_SQL


def migrate_201_to_202(conn: sqlite3.Connection) -> None:
    """Create the fresh-read tables and their indexes."""
    has_messages = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()
    for statement in FRESH_READ_SCHEMA_SQL.split(";"):
        if statement.strip() and (has_messages or statement.strip() != PERSON_INDEX_SQL):
            conn.execute(statement)
