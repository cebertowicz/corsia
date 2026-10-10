"""Schema 86 -> 87: record which reply a user saw in each comment thread."""

from __future__ import annotations

import sqlite3


def migrate_86_to_87(conn: sqlite3.Connection) -> None:
    """Replace document-level view stamps with per-root stamps.

    Each document stamp becomes a stamp on every root comment of that document,
    so no thread changes waiting state. The seen reply is the newest reply at
    exactly the old instant, keeping a reply at that instant seen as before.
    Comment rows are never hard-deleted (threads resolve), so no cascade.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS doc_comment_thread_views (
            user_id TEXT NOT NULL,
            root_id TEXT NOT NULL REFERENCES doc_comments(id),
            seen_at TEXT NOT NULL,
            seen_reply_id TEXT NOT NULL,
            PRIMARY KEY (user_id, root_id)
        )"""
    )
    legacy = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='doc_comment_views'",
    ).fetchone()
    if legacy is None:
        return
    conn.execute(
        """INSERT OR IGNORE INTO doc_comment_thread_views(user_id,root_id,seen_at,seen_reply_id)
           SELECT view.user_id, root.id, view.viewed_at,
                  COALESCE((SELECT MAX(reply.id) FROM doc_comments reply
                            WHERE reply.parent_id=root.id
                              AND reply.created_at=view.viewed_at), '')
           FROM doc_comment_views view
           JOIN doc_comments root
             ON root.artifact_id=view.artifact_id AND root.parent_id IS NULL"""
    )
    conn.execute("DROP TABLE doc_comment_views")
