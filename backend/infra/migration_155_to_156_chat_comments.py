"""Schema 156: locate sent chat comments by their target message."""

import json
import sqlite3


def migrate_155_to_156(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS chat_comments (
        thread_id TEXT NOT NULL, target_message_id TEXT NOT NULL,
        user_message_id TEXT NOT NULL, cid TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY (user_message_id, cid)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_comments_target ON chat_comments(thread_id, target_message_id)")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_comment_answers'").fetchone():
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_comment_answers_comment ON chat_comment_answers(thread_id, user_message_id, cid)")
    # A partial older database may have no messages to backfill.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone():
        return
    rows = conn.execute("SELECT id,thread_id,metadata,created_at FROM messages WHERE role='user' AND metadata LIKE '%reply_context%' AND metadata LIKE '%comments%'")
    for message_id, thread_id, metadata, created_at in rows:
        try:
            comments = json.loads(metadata).get('reply_context', {}).get('comments', [])
        except (ValueError, AttributeError, TypeError):
            continue
        if not isinstance(comments, list):
            continue
        for comment in comments:
            if not isinstance(comment, dict) or not isinstance(comment.get('cid'), str) or not isinstance(comment.get('seq'), int):
                continue
            conn.execute(
                "INSERT OR IGNORE INTO chat_comments (thread_id,target_message_id,user_message_id,cid,created_at) "
                "SELECT ?,id,?,?,? FROM messages WHERE thread_id=? AND seq=? AND role='assistant'",
                (thread_id, message_id, comment['cid'], created_at, thread_id, comment['seq']),
            )
