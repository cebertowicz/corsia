"""Activity's Notifications rows: one row per document with unread replies to the person.

Requires a sqlite3 connection configured with sqlite3.Row, as in the backend.
No writes are made; the read reuses the canonical unread SQL, so a row's count is
the same unread set the document pane and its jump button count.
"""
from __future__ import annotations

from doceditor.feedback_state import unread_comment_rows
from infra.activity_queue_base import clip_title, flat as _flat
from infra.auth import may_access_artifact
from plans.public import UNKNOWN_THREAD_SOURCE, thread_sources


def _agent(source: dict) -> str:
    """An untitled or unknown thread is still an agent; never call it 'Untitled thread replied'."""
    label = source.get('thread_label')
    return 'An agent' if not source.get('thread_id') or label in (None, '', 'Untitled thread', UNKNOWN_THREAD_SOURCE['thread_label']) else label


def document_rows(db, user_id: str) -> list[dict]:
    """Return one row per document holding an unread agent reply to the person's comment.

    A document qualifies when an agent answered in a thread the person commented in, and
    the answer is unread. Its count is every unread item in the document (the pane's set),
    so the row, the pane's bar and its jump button never disagree. The caller decides
    where a reviewed document's count goes; this read does not drop it.
    """
    unread_sql, unread_params = unread_comment_rows(user_id, None)
    # The attention authorization seam requires a current routing owner; a
    # released owner's registration is not an owner, even if comments can read it.
    sql = f"""
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
             (item.parent_id IS NOT NULL AND item.author_kind='agent'
              AND item.artifact_id=root.artifact_id
              AND EXISTS (SELECT 1 FROM threads author WHERE author.id=item.author_thread_id AND author.user_id=?)
              AND EXISTS (SELECT 1 FROM doc_comments person WHERE person.artifact_id=root.artifact_id
                          AND person.author_kind='user' AND (person.id=root.id OR person.parent_id=root.id))
             ) AS qualifies
      {unread_sql}
    ), counted AS (
      SELECT u.*, COUNT(*) OVER (PARTITION BY u.artifact_id) AS unread FROM unread u
    )
    SELECT c.root_id, c.reply_id, c.artifact_id, c.created_at, c.author_thread_id, c.unread,
           i.title AS artifact_title, own.parent_thread_id AS owner_thread
    FROM counted c
    JOIN artifact_identities i ON i.id=c.artifact_id AND i.state='active'
    JOIN owner_candidates own ON own.artifact_id=i.id AND own.pos=1
    WHERE c.qualifies AND own.parent_thread_id IS NOT NULL AND own.parent_thread_id<>''
    ORDER BY c.artifact_id, c.created_at DESC, c.reply_id DESC
    """
    # Access is the artifact routes' own predicate, asked once per owner thread.
    access: dict[str, bool] = {}

    def allowed(owner: str) -> bool:
        if owner not in access:
            access[owner] = may_access_artifact(owner, user_id, db=db)
        return access[owner]

    replies = [row for row in db.execute(sql, (user_id, *unread_params)).fetchall()
               if allowed(row['owner_thread'])]
    sources = thread_sources(db, {str(r['author_thread_id']) for r in replies})
    groups: dict[str, list] = {}
    for row in replies:  # newest first within each document
        groups.setdefault(row['artifact_id'], []).append(row)
    result = []
    for artifact, rows in groups.items():
        latest = rows[0]
        threads: dict[str, str] = {}
        agents: list[str] = []
        for row in rows:
            threads.setdefault(row['root_id'], row['reply_id'])
            label = _agent(sources[row['author_thread_id']])
            if label not in agents:
                agents.append(label)
        result.append({
            'renderer': 'document', 'kind': 'document', 'key': f'doc:{artifact}',
            'external_id': f'doc:{artifact}',
            'title': clip_title(_flat(latest['artifact_title'])) or 'A document',
            'raised_at': latest['created_at'], 'source': sources[latest['author_thread_id']],
            'open': {'artifact_id': artifact, 'comment_id': latest['root_id']},
            'detail': {'artifact_id': artifact, 'unread': latest['unread'], 'latest_reply_id': latest['reply_id'],
                       'agents': agents, 'threads': [{'root_id': root, 'reply_id': reply} for root, reply in threads.items()]},
            'priority': 0,
        })
    return result
