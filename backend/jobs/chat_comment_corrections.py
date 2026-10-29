"""Select and hydrate indexed chat comment correction sources."""
from __future__ import annotations

import json

from core.corrections_input import agent_context, owner_text
from core.corrections_recording import (
    CHAT_COMMENT_SUBJECT_PREFIX, CORRECTION_EVENT, SOURCE_KIND_CHAT_COMMENT,
    CorrectionCandidate, is_wrapper_or_delivery, owner_message_sql_predicate, version_match_sql,
)
from core.measurement_scope import scope_cte
from jobs.corrections_scanning import PendingCandidateKey, _before_clause, _before_params


def chat_comment_source_id(message_id: str, cid: str) -> str:
    """Encode the colon-free comment ID before its sending message ID."""
    return f"{cid}:{message_id}"


def pending_chat_comment_keys(version, batch_size, owner_user_id, before=None, since=None):
    """Select one owner's unrecorded indexed notes in the shared scan order."""
    from infra.database import get_db

    db = get_db()
    try:
        owner_filter, owner_params = owner_message_sql_predicate(allow_blank=True)
        version_filter, version_params = version_match_sql("e", version)
        identity = "c.cid || ':' || c.user_message_id"
        rows = db.execute(scope_cte() + f"""
            SELECT {identity},c.created_at FROM chat_comments c
            JOIN messages m ON m.id=c.user_message_id AND m.thread_id=c.thread_id
            JOIN scope_threads scoped ON scoped.id=m.thread_id
            JOIN json_each(CASE WHEN json_valid(m.metadata) THEN m.metadata ELSE '{{}}' END,
                           '$.reply_context.comments') item
              ON json_extract(item.value,'$.cid')=c.cid
            WHERE {owner_filter}
              AND length(trim(COALESCE(json_extract(item.value,'$.note'),''), ' ' || char(9,10,13)))>0
              AND {_before_clause(SOURCE_KIND_CHAT_COMMENT, identity, 'c.created_at')}
              AND (:since_created_at IS NULL OR c.created_at >= :since_created_at)
              AND NOT EXISTS (SELECT 1 FROM outcome_events e WHERE e.event_name=:event
                  AND e.subject_key=:prefix || {identity} AND e.retracted_at IS NULL AND {version_filter})
            ORDER BY c.created_at DESC,{identity} DESC LIMIT :scan_limit
        """, {**owner_params, **version_params, **_before_params(before),
              'source_kind': SOURCE_KIND_CHAT_COMMENT, 'user_id': owner_user_id,
              'scope_project': 'all', 'scope_agent': None, 'since_created_at': since,
              'event': CORRECTION_EVENT, 'prefix': CHAT_COMMENT_SUBJECT_PREFIX,
              'scan_limit': batch_size}).fetchall()
        return [PendingCandidateKey(None, SOURCE_KIND_CHAT_COMMENT, str(row[0]), None, None, str(row[1])) for row in rows]
    finally:
        db.close()


def notify_chat_comments(message_id: str) -> None:
    """Queue committed indexed comments; hydration rechecks the author and note."""
    from infra.database import get_db
    from jobs.corrections_realtime import notify

    db = get_db()
    try:
        ids = [chat_comment_source_id(message_id, str(row[0])) for row in db.execute(
            "SELECT cid FROM chat_comments WHERE user_message_id=?", (message_id,),
        )]
    finally:
        db.close()
    for source_id in ids:
        notify(SOURCE_KIND_CHAT_COMMENT, source_id)


def chat_comment_candidate(db, source_id: str) -> CorrectionCandidate | None:
    """Build a source from its committed index entry and sending message."""
    cid, separator, message_id = source_id.partition(':')
    if not separator:
        return None
    row = db.execute("""SELECT c.cid,c.created_at,m.role,m.content,m.metadata,m.thread_id,t.project_id
        FROM chat_comments c JOIN messages m ON m.id=c.user_message_id AND m.thread_id=c.thread_id
        JOIN threads t ON t.id=m.thread_id WHERE c.user_message_id=? AND c.cid=?""", (message_id, cid)).fetchone()
    if not row or row['role'] != 'user' or is_wrapper_or_delivery(row['content'] or '', row['metadata']):
        return None
    try:
        comments = json.loads(row['metadata'] or '{}').get('reply_context', {}).get('comments', [])
        comment = next(item for item in comments if item.get('cid') == row['cid'])
        note = comment.get('note')
        if not isinstance(note, str) or not note.strip():
            return None
    except (TypeError, ValueError, AttributeError, StopIteration):
        return None
    return CorrectionCandidate(SOURCE_KIND_CHAT_COMMENT, source_id, str(row['thread_id']), row['project_id'],
                               owner_text(note), str(row['created_at']), context=agent_context(comment.get('quote') or ''),
                               quote=note, about='chat comment')
