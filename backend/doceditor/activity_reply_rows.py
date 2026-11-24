"""Activity reply rows; integrate in the backend with the existing queue projection.

Requires a sqlite3 connection configured with sqlite3.Row, as in the backend.
No writes are made. The document and chat reads reuse their canonical unread SQL.
"""
from __future__ import annotations

from doceditor.feedback_state import unread_comment_rows
from infra.chat_comment_answers import UNREAD_FROM, UNREAD_WHERE
from plans.attention_detail import _sources


def _title(text: str | None, fallback: str) -> str:
    return " ".join((text or "").split())[:160] or fallback


def reply_rows(db, user_id: str, *, reviewed_artifact_ids: set[str]) -> list[dict]:
    """Return actionable unread agent replies visible to this user.

    Document rows are grouped by comment root; chat rows are grouped by the
    comment whose viewed endpoint advances the watermark. The newest unread
    answer is the one that can advance that watermark over its predecessors.
    Pass artifact IDs from review rows actually emitted and authorized for this
    user, before applying any response cap.
    """
    unread_sql, unread_params = unread_comment_rows(user_id, None)
    # The attention authorization seam requires a current routing owner; a
    # released owner's registration is not an owner, even if comments can read it.
    document_sql = f"""
    WITH owner_candidates AS (
      SELECT r.artifact_id, r.parent_thread_id,
             ROW_NUMBER() OVER (PARTITION BY r.artifact_id ORDER BY
               CASE r.relationship_state WHEN 'owner-pinned' THEN 0 ELSE 1 END,
               r.updated_at DESC, r.id ASC) AS pos
      FROM artifact_registrations r
      WHERE r.relationship_state IN ('owner-pinned','owner-active')
    ), unread AS (
      SELECT root.id AS root_id, item.id AS reply_id, item.artifact_id,
             item.created_at, item.author_thread_id,
             ROW_NUMBER() OVER (PARTITION BY root.id ORDER BY item.created_at DESC,item.id DESC) AS pos
      {unread_sql}
      AND item.parent_id IS NOT NULL AND item.author_kind='agent'
      AND item.artifact_id=root.artifact_id
      AND EXISTS (SELECT 1 FROM threads author WHERE author.id=item.author_thread_id AND author.user_id=?)
      AND EXISTS (SELECT 1 FROM doc_comments person WHERE person.artifact_id=root.artifact_id
                   AND person.author_kind='user' AND (person.id=root.id OR person.parent_id=root.id))
    )
    SELECT u.*, i.title AS artifact_title FROM unread u
    JOIN artifact_identities i ON i.id=u.artifact_id AND i.state='active'
    JOIN owner_candidates own ON own.artifact_id=i.id AND own.pos=1
    LEFT JOIN threads owner ON owner.id=own.parent_thread_id
    WHERE u.pos=1 AND own.parent_thread_id IS NOT NULL AND own.parent_thread_id<>''
      AND (own.parent_thread_id='ui' OR substr(own.parent_thread_id,1,8)='project:' OR owner.user_id=?)
    """
    documents = db.execute(document_sql, (*unread_params, user_id, user_id)).fetchall()
    chats = db.execute(f"""
      WITH unread AS (
        SELECT a.thread_id, a.user_message_id, a.cid, a.assistant_message_id,
               a.answered_at, c.target_message_id,
               COALESCE(c.root_user_message_id,a.user_message_id) AS root_user_message_id,
               COALESCE(c.root_cid,a.cid) AS root_cid,
               ROW_NUMBER() OVER (PARTITION BY a.thread_id,a.user_message_id,a.cid
                                  ORDER BY a.answered_at DESC,a.assistant_message_id DESC) AS pos
        {UNREAD_FROM}
        WHERE {UNREAD_WHERE}
      ) SELECT * FROM unread WHERE pos=1
    """, (user_id, user_id)).fetchall()
    sources = _sources(db, {str(r['author_thread_id']) for r in documents} | {str(r['thread_id']) for r in chats})
    result = []
    for row in documents:
        artifact, root, reply = row['artifact_id'], row['root_id'], row['reply_id']
        if artifact in reviewed_artifact_ids:
            continue
        result.append({
            'renderer': 'reply', 'kind': 'reply', 'key': f'doc-reply:{artifact}:{root}',
            'external_id': f'doc-reply:{artifact}:{root}',
            'title': _title(row['artifact_title'], 'Document reply'), 'raised_at': row['created_at'],
            'source': sources[row['author_thread_id']], 'open': {'artifact_id': artifact, 'comment_id': root},
            'detail': {'artifact_id': artifact, 'root_id': root, 'reply_id': reply}, 'priority': 0,
        })
    for row in chats:
        thread, message, cid = row['thread_id'], row['user_message_id'], row['cid']
        key = f'chat-reply:{thread}:{message}:{cid}'
        source = sources[thread]
        result.append({
            'renderer': 'reply', 'kind': 'reply', 'key': key, 'external_id': key,
            'title': _title(source['thread_label'], 'Chat reply'), 'raised_at': row['answered_at'],
            'source': source, 'open': {'thread_id': thread, 'message_id': row['target_message_id']},
            'detail': {'thread_id': thread, 'message_id': message, 'cid': cid,
                       'assistant_message_id': row['assistant_message_id'],
                       'root_user_message_id': row['root_user_message_id'], 'root_cid': row['root_cid']},
            'priority': 0,
        })
    return result
