"""Schema 126 -> 127: index user comments by time for the corrections scan.

The corrections scan reads its window's user comments that have no fact yet,
newest first (``jobs.corrections_scanning``); this index bounds that read to
the window instead of every comment ever written.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

from infra.doc_comments_schema import USER_COMMENTS_BY_TIME_INDEX_SQL


def migrate_126_to_127(conn: sqlite3.Connection) -> None:
    """Add the user-comments-by-time index where the comments table exists."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='doc_comments'").fetchone() is not None:
        conn.execute(USER_COMMENTS_BY_TIME_INDEX_SQL)
