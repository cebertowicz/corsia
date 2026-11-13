"""Unread chat reply projection for the displayed thread."""

from __future__ import annotations

from infra.chat_comment_answers import UNREAD_FROM, UNREAD_WHERE


def chat_reply_items(db, user_id: str, thread_id: str) -> list[dict]:
    """Read unread chat replies only for the displayed thread."""
    params = (user_id, user_id, thread_id)
    rows = db.execute(f"""WITH unread AS (
        SELECT a.thread_id,COALESCE(c.root_user_message_id,a.user_message_id) AS user_message_id,
               COALESCE(c.root_cid,a.cid) AS cid,a.answered_at,c.target_message_id,target.seq AS target_seq,
               ROW_NUMBER() OVER (PARTITION BY a.thread_id ORDER BY a.answered_at,a.user_message_id,a.cid) AS position,
               COUNT(*) OVER (PARTITION BY a.thread_id) AS unread_count,
               MAX(a.answered_at) OVER (PARTITION BY a.thread_id) AS newest_at
          {UNREAD_FROM}
         WHERE {UNREAD_WHERE} AND a.thread_id=?
    ) SELECT * FROM unread WHERE position=1""", params).fetchall()
    return [{'detail': {
        'title': '', 'unread_count': row['unread_count'], 'newest_at': row['newest_at'],
        'destination': {'pane': 'chat-replies', 'id': row['target_message_id'],
                        'user_message_id': row['user_message_id'], 'cid': row['cid'], 'seq': row['target_seq']},
        'source': {'thread_id': row['thread_id']},
    }} for row in rows]
