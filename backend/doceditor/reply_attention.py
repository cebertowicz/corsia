"""Unread chat reply projection for the displayed thread."""

from __future__ import annotations

from infra.chat_comment_answers import UNREAD_FROM, UNREAD_WHERE
from infra.message_update_schema import not_replaced_sql


def chat_reply_items(db, user_id: str, thread_id: str) -> list[dict]:
    """Read unread chat replies only for the displayed thread.

    Two kinds of item count, each as one: an unread agent answer in a comment thread, and an unread
    agent root (a comment an agent left on its own message; unread until the person opens it at or
    after its ``updated_at``). A replaced target hides both.
    """
    params = (user_id, user_id, thread_id, user_id, user_id, thread_id)
    rows = db.execute(f"""WITH items AS (
        SELECT a.thread_id,COALESCE(c.root_user_message_id,a.user_message_id) AS user_message_id,
               COALESCE(c.root_cid,a.cid) AS cid,a.answered_at,c.target_message_id,target.seq AS target_seq,
               a.user_message_id AS item_sender,a.cid AS item_cid
          {UNREAD_FROM}
         WHERE {UNREAD_WHERE} AND a.thread_id=?
        UNION ALL
        SELECT r.thread_id,r.root_id,r.cid,r.updated_at,r.target_message_id,target.seq,r.root_id,r.cid
          FROM chat_comment_agent_roots r
          JOIN threads t ON t.id=r.thread_id AND t.user_id=?
          JOIN messages target ON target.id=r.target_message_id AND target.thread_id=r.thread_id
               AND {not_replaced_sql('target.id')}
          LEFT JOIN chat_comment_views v ON v.user_id=? AND v.user_message_id=r.root_id AND v.cid=r.cid
         WHERE (v.user_id IS NULL OR v.seen_at < r.updated_at) AND r.thread_id=?
    ), unread AS (
        SELECT items.*,
               ROW_NUMBER() OVER (PARTITION BY thread_id ORDER BY answered_at,item_sender,item_cid) AS position,
               COUNT(*) OVER (PARTITION BY thread_id) AS unread_count,
               MAX(answered_at) OVER (PARTITION BY thread_id) AS newest_at
          FROM items
    ) SELECT * FROM unread WHERE position=1""", params).fetchall()
    return [{'detail': {
        'title': '', 'unread_count': row['unread_count'], 'newest_at': row['newest_at'],
        'destination': {'pane': 'chat-replies', 'id': row['target_message_id'],
                        'user_message_id': row['user_message_id'], 'cid': row['cid'], 'seq': row['target_seq']},
        'source': {'thread_id': row['thread_id']},
    }} for row in rows]
