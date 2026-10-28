"""Index sent chat comments by their target message."""

from __future__ import annotations

import json
import sqlite3

from infra.chat_comment_answers import parse_answers


def place_comments(db: sqlite3.Connection, thread_id: str, user_message_id: str,
                   metadata: dict | None, created_at: str) -> None:
    """Place validated comments in the transaction that stores the input."""
    comments = (metadata or {}).get('reply_context', {}).get('comments', [])
    for comment in comments:
        db.execute(
            "INSERT OR IGNORE INTO chat_comments "
            "(thread_id,target_message_id,user_message_id,cid,created_at) "
            "SELECT ?,id,?,?,? FROM messages WHERE thread_id=? AND seq=? AND role='assistant'",
            (thread_id, user_message_id, comment['cid'], created_at, thread_id, comment['seq']),
        )


def attach_comments(db: sqlite3.Connection, thread_id: str, user_id: str, messages: list[dict]) -> None:
    """Attach comments and indexed answers for the targets in one loaded page."""
    by_id = {message['id']: message for message in messages if message.get('role') == 'assistant'}
    if not by_id:
        return
    rows = db.execute(
        "SELECT c.target_message_id,c.user_message_id,c.cid,c.created_at,"
        "a.assistant_message_id,a.answered_at,m.content,v.seen_at "
        "FROM chat_comments c "
        "LEFT JOIN chat_comment_answers a ON a.user_message_id=c.user_message_id AND a.cid=c.cid "
        "LEFT JOIN messages m ON m.id=a.assistant_message_id AND m.thread_id=c.thread_id "
        "LEFT JOIN chat_comment_views v ON v.user_id=? AND v.user_message_id=c.user_message_id AND v.cid=c.cid "
        "WHERE c.thread_id=? AND c.target_message_id IN (SELECT value FROM json_each(?)) "
        "ORDER BY c.created_at,c.user_message_id,c.cid,a.answered_at",
        (user_id, thread_id, json.dumps(list(by_id))),
    ).fetchall()
    answer_rows = db.execute(
        "SELECT a.assistant_message_id,a.cid FROM chat_comment_answers a "
        "JOIN chat_comments c ON c.user_message_id=a.user_message_id AND c.cid=a.cid AND c.thread_id=a.thread_id "
        "JOIN messages sender ON sender.id=a.user_message_id AND sender.thread_id=a.thread_id "
        "JOIN messages target ON target.id=c.target_message_id AND target.thread_id=a.thread_id "
        "WHERE a.thread_id=? AND a.assistant_message_id IN (SELECT value FROM json_each(?))",
        (thread_id, json.dumps(list(by_id))),
    ).fetchall()
    for answer_id, cid in answer_rows:
        by_id[answer_id].setdefault('chat_answer_cids', []).append(cid)
    sender_ids = list({row[1] for row in rows})
    if not sender_ids:
        return
    senders = db.execute(
        "SELECT id,metadata FROM messages WHERE thread_id=? AND id IN (SELECT value FROM json_each(?))",
        (thread_id, json.dumps(sender_ids)),
    ).fetchall()
    comments = {}
    for sender_id, metadata in senders:
        for comment in json.loads(metadata or '{}').get('reply_context', {}).get('comments', []):
            comments[(sender_id, comment['cid'])] = comment
    threads = {}
    for target, sender, cid, created, answer_id, answered_at, text, seen in rows:
        comment = comments.get((sender, cid))
        if comment is None:
            continue
        key = (sender, cid)
        if key not in threads:
            threads[key] = {**comment, 'user_message_id': sender, 'created_at': created, 'answers': []}
            by_id[target].setdefault('chat_comments', []).append(threads[key])
        if answer_id and text is not None:
            sections = parse_answers(text, answer_id).get(cid, [])
            if sections:
                threads[key]['answers'].append({
                    'assistant_message_id': answer_id,
                    'text': '\n\n'.join(section['text'] for section in sections),
                    'created_at': answered_at, 'unread': seen is None or seen < answered_at,
                })
    for message in by_id.values():
        if 'chat_comments' in message:
            message['chat_comments'].sort(key=lambda comment: comment['from_off'])
