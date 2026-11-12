"""Index sent chat comments by their target message."""

from __future__ import annotations

import json
import sqlite3

from infra.chat_comment_answers import UNREAD_FROM, UNREAD_WHERE, parse_answers, saved_answer_fields
from infra.delivery_retry import newest_attempt_delivery


def comment_only(content: str | None, metadata: dict | None) -> bool:
    """A user input that carries chat comments and nothing else: no text, no attachments.

    Its comments are its whole content, so chat draws nothing for it; it lives in
    its comment thread only. The 189 -> 190 data step reads the same rule in SQL:
    comments are a non-empty list, and attachments are absent, null or an empty list.
    """
    metadata = metadata if isinstance(metadata, dict) else {}
    reply_context = metadata.get('reply_context')
    comments = reply_context.get('comments') if isinstance(reply_context, dict) else None
    attachments = metadata.get('attachments')
    return (isinstance(comments, list) and bool(comments) and attachments in (None, [])
            and not (content or '').strip())


def place_comments(db: sqlite3.Connection, thread_id: str, user_message_id: str,
                   metadata: dict | None, created_at: str) -> None:
    """Place validated comments in the transaction that stores the input.

    A retry replaces its original's comments that have no answer and no reply;
    any other keeps its thread beside the retry's. A comment-only input also
    records that it draws nothing in chat (an empty `chat_visible_content`).
    """
    retried_from = (metadata or {}).get('retried_from_message_id')
    if retried_from:
        db.execute(
            "DELETE FROM chat_comments WHERE thread_id=? AND user_message_id=? AND NOT EXISTS "
            "(SELECT 1 FROM chat_comment_answers a WHERE a.thread_id=chat_comments.thread_id "
            "AND a.user_message_id=chat_comments.user_message_id AND a.cid=chat_comments.cid) "
            "AND NOT EXISTS (SELECT 1 FROM chat_comments r WHERE r.thread_id=chat_comments.thread_id "
            "AND r.root_user_message_id=chat_comments.user_message_id AND r.root_cid=chat_comments.cid)",
            (thread_id, retried_from),
        )
    comments = (metadata or {}).get('reply_context', {}).get('comments', [])
    for comment in comments:
        db.execute(
            "INSERT OR IGNORE INTO chat_comments "
            "(thread_id,target_message_id,user_message_id,cid,created_at,root_user_message_id,root_cid) "
            "SELECT ?,id,?,?,?,?,? FROM messages WHERE thread_id=? AND seq=? AND role='assistant'",
            (thread_id, user_message_id, comment['cid'], created_at,
             comment.get('root_user_message_id'), comment.get('root_cid'), thread_id, comment['seq']),
        )
    stored = db.execute("SELECT content FROM messages WHERE id=?", (user_message_id,)).fetchone() if comments else None
    if stored is not None and comment_only(stored[0], metadata):
        db.execute(
            "INSERT OR REPLACE INTO chat_visible_content(message_id,thread_id,content) VALUES (?,?,'')",
            (user_message_id, thread_id),
        )


def _merge_agent_answer(agent_answers: dict, thread: dict, key: tuple, sender: str, cid: str,
                        answer_id: str, answered_at: str, text: str, unread: int) -> None:
    """Add this cid's sections to the thread's one answer from this agent message."""
    sections = [section['text'] for section in parse_answers(text, answer_id).get(cid, [])]
    if not sections:
        return
    answer = agent_answers.get((key, answer_id))
    if answer is None:
        agent_answers[(key, answer_id)] = answer = {
            'author_kind': 'agent', 'user_message_id': sender, 'cid': cid,
            'assistant_message_id': answer_id, 'sections': [],
            'created_at': answered_at, 'unread': False,
        }
        thread['answers'].append(answer)
    answer['sections'] += [section for section in sections if section not in answer['sections']]
    answer['unread'] = answer['unread'] or bool(unread)


def attach_comments(db: sqlite3.Connection, thread_id: str, user_id: str, messages: list[dict]) -> None:
    """Attach comments and indexed answers for the targets in one loaded page."""
    by_id = {message['id']: message for message in messages if message.get('role') == 'assistant'}
    if not by_id:
        return
    rows = db.execute(
        "SELECT c.target_message_id,c.user_message_id,c.cid,c.created_at,"
        "answer.assistant_message_id,answer.answered_at,m.content,c.root_user_message_id,c.root_cid,"
        f"EXISTS (SELECT 1 {UNREAD_FROM} WHERE {UNREAD_WHERE} "
        "AND a.cid=answer.cid AND a.assistant_message_id=answer.assistant_message_id) "
        "FROM chat_comments c "
        "LEFT JOIN chat_comment_answers answer ON answer.thread_id=c.thread_id AND answer.user_message_id=c.user_message_id AND answer.cid=c.cid "
        "LEFT JOIN messages m ON m.id=answer.assistant_message_id AND m.thread_id=c.thread_id "
        "WHERE c.thread_id=? AND c.target_message_id IN (SELECT value FROM json_each(?)) "
        "ORDER BY c.created_at,c.user_message_id,c.cid,answer.answered_at",
        (user_id, user_id, thread_id, json.dumps(list(by_id))),
    ).fetchall()
    for message_id, fields in saved_answer_fields(db, thread_id, list(by_id)).items():
        by_id[message_id].update(fields)
    sender_ids = list({row[1] for row in rows})
    if not sender_ids:
        return
    senders = db.execute(
        "SELECT id,metadata,delivery_state,delivery_error FROM messages "
        "WHERE thread_id=? AND id IN (SELECT value FROM json_each(?))",
        (thread_id, json.dumps(sender_ids)),
    ).fetchall()
    comments = {}
    # A comment is its input: it shows the delivery of that input's newest Retry copy, so a failure stays in
    # its thread with Retry until a copy is delivered, including where the copy got a thread of its own.
    for sender_id, metadata, delivery_state, delivery_error in senders:
        if delivery_state in ('failed', 'ambiguous'):
            delivery_state, delivery_error = newest_attempt_delivery(db, thread_id, sender_id, delivery_state, delivery_error)
        for comment in json.loads(metadata or '{}').get('reply_context', {}).get('comments', []):
            comments[(sender_id, comment['cid'])] = {
                **comment, 'delivery_state': delivery_state, 'delivery_error': delivery_error,
            }
    threads = {}
    for target, sender, cid, created, _, _, _, root_sender, _, _ in rows:
        comment = comments.get((sender, cid))
        if root_sender is None and comment is not None and (sender, cid) not in threads:
            threads[(sender, cid)] = {**comment, 'user_message_id': sender, 'created_at': created, 'answers': []}
            by_id[target].setdefault('chat_comments', []).append(threads[(sender, cid)])
    replies_added = set()
    # One agent message gives a thread one answer, however many of the thread's cids its tags name.
    agent_answers: dict[tuple, dict] = {}
    for target, sender, cid, created, answer_id, answered_at, text, root_sender, root_cid, unread in rows:
        comment = comments.get((sender, cid))
        key = (root_sender, root_cid) if root_sender else (sender, cid)
        if comment is None or key not in threads:
            continue
        if root_sender and (sender, cid) not in replies_added:
            threads[key]['answers'].append({
                'author_kind': 'user', 'user_message_id': sender, 'cid': cid,
                'text': comment['note'], 'created_at': created, 'unread': False,
                'delivery_state': comment['delivery_state'], 'delivery_error': comment['delivery_error'],
            })
            replies_added.add((sender, cid))
        if answer_id and text is not None:
            _merge_agent_answer(agent_answers, threads[key], key, sender, cid, answer_id, answered_at, text, unread)
    for answer in agent_answers.values():
        answer['text'] = '\n\n'.join(answer.pop('sections'))
    for thread in threads.values():
        thread['answers'].sort(key=lambda answer: answer['created_at'])
    for message in by_id.values():
        if 'chat_comments' in message:
            message['chat_comments'].sort(key=lambda comment: comment['from_off'])
