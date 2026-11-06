"""Schema 181 -> 182: an agent's ask for the person is a mark on the comment it belongs to.

``tags_person`` keeps ``doc_comment_tags`` for agent tags only, and lets the unread read test a
column on the row it already reads.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3


def migrate_181_to_182(conn: sqlite3.Connection) -> None:
    """Add the person-tag mark."""
    columns = {column[1] for column in conn.execute("PRAGMA table_info(doc_comments)")}
    if not columns:
        # A database whose comments table has not arrived yet: it is created on demand from
        # the schema fragment (infra/doc_comments_schema.py), which already carries the column.
        return
    if "tags_person" not in columns:
        conn.execute("ALTER TABLE doc_comments ADD COLUMN tags_person INTEGER NOT NULL DEFAULT 0")
