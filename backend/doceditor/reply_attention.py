"""User-visible unread reply projection, using the canonical comment and view stores."""

from __future__ import annotations

import json
from pathlib import Path

from core.thread_identity import root_visible_thread_ids
from doceditor.feedback_state import new_reply_rows
from infra.auth import is_owner_sentinel
from infra.chat_comment_answers import UNREAD_FROM, UNREAD_WHERE


def _removed(path: str) -> bool:
    try:
        Path(path).stat()
    except FileNotFoundError:
        return True
    except OSError:
        # A transient file error keeps the hand-off available for its retry surface.
        return False
    return False


def unread_reply_items(db, user_id: str) -> list[dict]:
    """Group unread agent replies by replying thread and document, after authorization.

    Each item's ``source.thread_id`` is the replying thread's visible root chat, so one chat
    and document can carry several items. An unresolved lineage keeps the replying thread.
    """
    source, params = new_reply_rows(user_id, None, include_author_thread=True)
    rows = db.execute(
        f"""SELECT root.artifact_id, reply.author_thread_id, COUNT(*) AS unread_count, MAX(reply.created_at) AS newest_at
              {source}
               AND reply.author_kind='agent' AND thread.user_id=?
              GROUP BY root.artifact_id, reply.author_thread_id
              ORDER BY newest_at DESC, root.artifact_id, reply.author_thread_id""",
        (*params, user_id),
    ).fetchall()
    if not rows:
        return []
    roots = root_visible_thread_ids([str(row['author_thread_id']) for row in rows], db=db)
    ids = json.dumps(list({str(row['artifact_id']) for row in rows}))
    artifacts = db.execute(
        """SELECT i.id, i.title, i.path, r.parent_thread_id, owner.user_id AS owner_user_id,
                  ROW_NUMBER() OVER (PARTITION BY i.id ORDER BY
                      CASE r.relationship_state WHEN 'owner-pinned' THEN 0 ELSE 1 END,
                      r.updated_at DESC, r.id ASC) AS owner_rank
             FROM artifact_identities i
             JOIN artifact_registrations r ON r.artifact_id=i.id
             LEFT JOIN threads owner ON owner.id=r.parent_thread_id
            WHERE i.id IN (SELECT value FROM json_each(?)) AND i.state='active' AND i.type='document'
              AND r.relationship_state IN ('owner-pinned', 'owner-active')""",
        (ids,),
    ).fetchall()
    by_id = {str(row['id']): row for row in artifacts if row['owner_rank'] == 1}
    result = []
    for row in rows:
        artifact = by_id.get(str(row['artifact_id']))
        if not artifact:
            continue
        owner, path = artifact['parent_thread_id'], artifact['path']
        if not owner or not (is_owner_sentinel(owner) or artifact['owner_user_id'] == user_id) or not path or _removed(path):
            continue
        title = artifact['title'] or Path(path).name
        result.append({
            'detail': {
                'title': title, 'unread_count': row['unread_count'], 'newest_at': row['newest_at'],
                'destination': {'pane': 'document-replies', 'id': row['artifact_id'], 'path': path, 'title': title},
                'source': {'thread_id': roots[str(row['author_thread_id'])] or row['author_thread_id']},
            },
        })
    return result


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
