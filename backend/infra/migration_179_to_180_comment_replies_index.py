"""Schema 179 -> 180: index document comment replies by their thread.

Deciding a suggestion reads whether it has replies, and reply routing reads a
thread's replies (``doceditor.comment_recipients``); this index bounds each
read to that thread instead of every comment ever written.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

from infra.doc_comments_schema import REPLIES_BY_PARENT_INDEX_SQL


def migrate_179_to_180(conn: sqlite3.Connection) -> None:
    """Add the replies-by-parent index where the comments table has replies (a ``parent_id`` column)."""
    if any(column[1] == "parent_id" for column in conn.execute("PRAGMA table_info(doc_comments)")):
        conn.execute(REPLIES_BY_PARENT_INDEX_SQL)
