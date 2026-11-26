"""Chat comment replies in Activity, using chat's existing unread state."""
from __future__ import annotations

import json

from infra.chat_comment_answers import UNREAD_FROM, UNREAD_WHERE
from infra.chat_comment_index import attach_comments


def chat_rows(db, user_id: str) -> list[dict]:
    """One row per owned chat with unread answers; one aggregate, never a query per chat."""
    rows = db.execute(f"""
        SELECT a.thread_id, t.title, t.project_id, p.name AS project_name, t.spawned_by,
               COUNT(*) AS unread, MAX(a.answered_at) AS newest_at
        {UNREAD_FROM}
        LEFT JOIN projects p ON p.id=t.project_id
        WHERE {UNREAD_WHERE}
        GROUP BY a.thread_id
    """, (user_id, user_id)).fetchall()
    return [{
        'renderer': 'chat', 'kind': 'chat', 'key': f'chat-reply:{row["thread_id"]}',
        'external_id': f'chat-reply:{row["thread_id"]}',
        'title': (row['title'] or '').strip() or 'New Chat',
        'raised_at': row['newest_at'],
        'source': {'thread_id': row['thread_id'], 'thread_label': (row['title'] or '').strip() or 'New Chat',
                   'project_id': row['project_id'], 'project_label': row['project_name'],
                   'parent_reviewed': row['spawned_by'] is not None},
        'open': None, 'detail': {'thread_id': row['thread_id'], 'unread': row['unread'],
                                 'through': row['newest_at']}, 'priority': 0,
    } for row in rows]


def chat_detail(db, user_id: str, thread_id: str) -> list[dict]:
    """Load the messages and comment threads that currently contain unread answers."""
    rows = db.execute(f"""
        SELECT DISTINCT target.id, target.seq, target.content, target.created_at,
               COALESCE(c.root_user_message_id,c.user_message_id) AS root_user_message_id,
               COALESCE(c.root_cid,c.cid) AS root_cid
        {UNREAD_FROM}
        WHERE {UNREAD_WHERE} AND a.thread_id=?
        ORDER BY target.seq
    """, (user_id, user_id, thread_id)).fetchall()
    targets: dict[str, dict] = {}
    roots: dict[str, set[tuple[str, str]]] = {}
    for row in rows:
        targets.setdefault(row['id'], {'id': row['id'], 'role': 'assistant', 'seq': row['seq'],
                                       'content': row['content'], 'created_at': row['created_at']})
        roots.setdefault(row['id'], set()).add((row['root_user_message_id'], row['root_cid']))
    messages = list(targets.values())
    attach_comments(db, thread_id, user_id, messages)
    for message in messages:
        wanted = roots[message['id']]
        message['chat_comments'] = [c for c in message.get('chat_comments', [])
                                    if (c['user_message_id'], c['cid']) in wanted]
    return [message for message in messages if message['chat_comments']]


def mark_chats_read(db, user_id: str, chats: list[dict]) -> None:
    """Advance chat's own stamps through the answer shown for each requested chat."""
    db.execute(f"""
        INSERT INTO chat_comment_views(user_id,user_message_id,cid,seen_at)
        SELECT ?, a.user_message_id, a.cid, MAX(a.answered_at)
        {UNREAD_FROM}
        JOIN json_each(?) requested ON a.thread_id=json_extract(requested.value,'$.thread_id')
        WHERE {UNREAD_WHERE} AND a.answered_at<=json_extract(requested.value,'$.through')
        GROUP BY a.user_message_id,a.cid
        ON CONFLICT(user_message_id,cid,user_id) DO UPDATE
          SET seen_at=MAX(seen_at,excluded.seen_at)
    """, (user_id, user_id, user_id, json.dumps(chats)))


def mark_chat_read(db, user_id: str, thread_id: str, through: str) -> None:
    mark_chats_read(db, user_id, [{'thread_id': thread_id, 'through': through}])
