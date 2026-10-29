"""Schema 166: a retried send's unanswered comments show once, on the retry."""

import sqlite3


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def migrate_165_to_166(conn: sqlite3.Connection) -> None:
    """Drop unanswered comment rows of a send that a later retry re-placed."""
    if not _has_table(conn, 'chat_comments') or not _has_table(conn, 'messages'):
        return
    answered = (
        "EXISTS (SELECT 1 FROM chat_comment_answers a WHERE a.thread_id=chat_comments.thread_id "
        "AND a.user_message_id=chat_comments.user_message_id AND a.cid=chat_comments.cid)"
        if _has_table(conn, 'chat_comment_answers') else "0"
    )
    conn.execute(
        "DELETE FROM chat_comments WHERE user_message_id IN ("
        "SELECT CASE WHEN json_valid(r.metadata) THEN json_extract(r.metadata,'$.retried_from_message_id') END "
        "FROM messages r WHERE r.thread_id=chat_comments.thread_id AND r.role='user' "
        "AND r.metadata LIKE '%retried_from_message_id%') "
        f"AND NOT {answered}"
    )
