"""Activity reply rows; integrate in the backend with the existing queue projection.

Requires a sqlite3 connection configured with sqlite3.Row, as in the backend.
No writes are made. The document and chat reads reuse their canonical unread SQL.
"""
from __future__ import annotations

import json

from doceditor.feedback_state import unread_comment_rows
from infra.activity_queue_base import TITLE_MAX_CHARS, clip_title, flat as _flat
from infra.auth import may_access_artifact
from infra.chat_comment_answers import UNREAD_FROM, UNREAD_WHERE
from plans.public import UNKNOWN_THREAD_SOURCE, thread_sources


def _agent(source: dict) -> str:
    """An untitled or unknown thread is still an agent; never call it 'Untitled thread replied'."""
    label = source.get('thread_label')
    return 'An agent' if not source.get('thread_id') or label in (None, '', 'Untitled thread', UNKNOWN_THREAD_SOURCE['thread_label']) else label


def _quoted(prefix: str, text: str | None) -> str | None:
    """Quote the person's comment, shortening the quote so the closing mark always shows."""
    text = _flat(text)
    if not text:
        return None
    room = TITLE_MAX_CHARS - len(prefix) - 2
    if room < 2:
        return None
    return f'{prefix}“{clip_title(text, room)}”'


def _reply_title(agent: str, row) -> str:
    """Say who answered which of the person's comments; a document's file name is not that."""
    quoted = _quoted(f'{agent} replied to your comment ', row['root_body']) if row['root_kind'] == 'user' else None
    return quoted or clip_title(f"{agent} replied in your comment thread on {_flat(row['artifact_title']) or 'a document'}")


def _chat_note(metadata: str | None, cid: str) -> str | None:
    """The person's own words for one chat comment, from the input that carried it."""
    try:
        comments = (json.loads(metadata or '{}').get('reply_context') or {}).get('comments') or []
    except (ValueError, AttributeError):
        return None
    return next((c.get('note') for c in comments if isinstance(c, dict) and c.get('cid') == cid), None)


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
             item.created_at, item.author_thread_id, root.body AS root_body, root.author_kind AS root_kind,
             ROW_NUMBER() OVER (PARTITION BY root.id ORDER BY item.created_at DESC,item.id DESC) AS pos
      {unread_sql}
      AND item.parent_id IS NOT NULL AND item.author_kind='agent'
      AND item.artifact_id=root.artifact_id
      AND EXISTS (SELECT 1 FROM threads author WHERE author.id=item.author_thread_id AND author.user_id=?)
      AND EXISTS (SELECT 1 FROM doc_comments person WHERE person.artifact_id=root.artifact_id
                   AND person.author_kind='user' AND (person.id=root.id OR person.parent_id=root.id))
    )
    SELECT u.*, i.title AS artifact_title, own.parent_thread_id AS owner_thread FROM unread u
    JOIN artifact_identities i ON i.id=u.artifact_id AND i.state='active'
    JOIN owner_candidates own ON own.artifact_id=i.id AND own.pos=1
    WHERE u.pos=1 AND own.parent_thread_id IS NOT NULL AND own.parent_thread_id<>''
    """
    # Access is the artifact routes' own predicate, so a change to it governs these rows too.
    documents = [row for row in db.execute(document_sql, (*unread_params, user_id)).fetchall()
                 if may_access_artifact(row['owner_thread'], user_id, db=db)]
    chats = db.execute(f"""
      WITH unread AS (
        SELECT a.thread_id, a.user_message_id, a.cid, a.assistant_message_id,
               a.answered_at, c.target_message_id, u.metadata AS user_metadata,
               COALESCE(c.root_user_message_id,a.user_message_id) AS root_user_message_id,
               COALESCE(c.root_cid,a.cid) AS root_cid,
               ROW_NUMBER() OVER (PARTITION BY a.thread_id,a.user_message_id,a.cid
                                  ORDER BY a.answered_at DESC,a.assistant_message_id DESC) AS pos
        {UNREAD_FROM}
        WHERE {UNREAD_WHERE}
      ) SELECT * FROM unread WHERE pos=1
    """, (user_id, user_id)).fetchall()
    sources = thread_sources(db, {str(r['author_thread_id']) for r in documents} | {str(r['thread_id']) for r in chats})
    result = []
    for row in documents:
        artifact, root, reply = row['artifact_id'], row['root_id'], row['reply_id']
        if artifact in reviewed_artifact_ids:
            continue
        result.append({
            'renderer': 'reply', 'kind': 'reply', 'key': f'doc-reply:{artifact}:{root}',
            'external_id': f'doc-reply:{artifact}:{root}',
            'title': _reply_title(_agent(sources[row['author_thread_id']]), row), 'raised_at': row['created_at'],
            'source': sources[row['author_thread_id']], 'open': {'artifact_id': artifact, 'comment_id': root},
            'detail': {'artifact_id': artifact, 'root_id': root, 'reply_id': reply}, 'priority': 0,
        })
    for row in chats:
        thread, message, cid = row['thread_id'], row['user_message_id'], row['cid']
        key = f'chat-reply:{thread}:{message}:{cid}'
        source = sources[thread]
        agent = _agent(source)
        title = _quoted(f'{agent} replied to your comment ', _chat_note(row['user_metadata'], cid))
        result.append({
            'renderer': 'reply', 'kind': 'reply', 'key': key, 'external_id': key,
            'title': title or f'{agent} replied to your comment in chat', 'raised_at': row['answered_at'],
            'source': source, 'open': {'thread_id': thread, 'message_id': row['target_message_id']},
            'detail': {'thread_id': thread, 'message_id': message, 'cid': cid,
                       'assistant_message_id': row['assistant_message_id'],
                       'root_user_message_id': row['root_user_message_id'], 'root_cid': row['root_cid']},
            'priority': 0,
        })
    return result
