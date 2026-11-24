"""Schema 203 -> 204: chat-comment words become searchable in history.

Creates ``comment_fts`` and its two triggers on ``chat_comments`` (``infra.db_fts._init_comment_fts``),
so every comment stored from now on is indexed as it is placed. Nothing else: comments stored
before this step are filled after startup, in short batches, by ``infra.comment_search_fill``,
so this step adds no reindex of message history to the startup write lock.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

from infra.db_fts import _init_comment_fts


def migrate_203_to_204(conn: sqlite3.Connection) -> None:
    """Create the comment index and its triggers where they are missing."""
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    # Every database at 203 has both tables; the checks keep a partial or repeated run harmless.
    if "chat_comments" in names and "messages" in names and "comment_fts" not in names:
        _init_comment_fts(conn)
