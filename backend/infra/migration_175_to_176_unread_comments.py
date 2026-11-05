"""Schema 175 -> 176: carry existing unread state through the expanded projection."""
from __future__ import annotations

import sqlite3


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def migrate_175_to_176(conn: sqlite3.Connection) -> None:
    """Stamp old resolved replies and eligible root comments, keeping newer views."""
    if conn.execute("PRAGMA user_version").fetchone()[0] >= 176:
        return
    if not all(_has_table(conn, name) for name in ('users', 'doc_comments', 'doc_comment_thread_views')):
        return
    owners = (
        "UNION SELECT DISTINCT user_id FROM threads WHERE user_id IS NOT NULL"
        if _has_table(conn, 'threads') else ""
    )
    conn.execute(
        f"""WITH people AS (
               SELECT id AS user_id FROM users
               {owners}
           ), candidates AS (
               SELECT root.id AS root_id, root.id AS item_id, root.created_at
               FROM doc_comments root
               WHERE root.parent_id IS NULL AND root.author_kind='agent'
                 AND root.replacement IS NULL AND root.review=0
                 AND EXISTS (SELECT 1 FROM doc_comments person
                             WHERE person.artifact_id=root.artifact_id AND person.author_kind='user'
                               AND person.created_at<root.created_at)
               UNION ALL
               SELECT root.id, reply.id, reply.created_at
               FROM doc_comments root JOIN doc_comments reply ON reply.parent_id=root.id
               WHERE root.resolved=1
           ), ranked AS (
               SELECT *, ROW_NUMBER() OVER (PARTITION BY root_id ORDER BY created_at DESC, item_id DESC) AS n
               FROM candidates
           )
           INSERT INTO doc_comment_thread_views(user_id,root_id,seen_at,seen_reply_id)
           SELECT people.user_id, ranked.root_id, ranked.created_at, ranked.item_id
           FROM people CROSS JOIN ranked WHERE ranked.n=1
           ON CONFLICT(user_id,root_id) DO UPDATE SET
               seen_at=excluded.seen_at, seen_reply_id=excluded.seen_reply_id
           WHERE excluded.seen_at>doc_comment_thread_views.seen_at
              OR (excluded.seen_at=doc_comment_thread_views.seen_at
                  AND excluded.seen_reply_id>doc_comment_thread_views.seen_reply_id)"""
    )
