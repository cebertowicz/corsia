"""User-visible unread reply projection, using the canonical comment and view stores."""

from __future__ import annotations

import json
from pathlib import Path

from core.thread_identity import root_visible_thread_ids
from doceditor.feedback_state import new_reply_rows
from infra.auth import is_owner_sentinel


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
    """Group unread agent replies by the replying thread and document, after authorization."""
    source, params = new_reply_rows(user_id, None, include_author_thread=True)
    rows = db.execute(
        f"""SELECT root.artifact_id, reply.author_thread_id, thread.title AS thread_label,
                   COUNT(*) AS unread_count, MAX(reply.created_at) AS newest_at
              {source}
               AND reply.author_kind='agent' AND thread.user_id=?
              GROUP BY root.artifact_id, reply.author_thread_id
              ORDER BY newest_at DESC, root.artifact_id, reply.author_thread_id""",
        (*params, user_id),
    ).fetchall()
    if not rows:
        return []
    roots = root_visible_thread_ids([str(row['author_thread_id']) for row in rows], db=db)
    root_ids = json.dumps(list({root for root in roots.values() if root is not None}))
    titles = {str(row['id']): row['title'] for row in db.execute(
        'SELECT id, title FROM threads WHERE id IN (SELECT value FROM json_each(?))', (root_ids,),
    ).fetchall()}
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
        root = roots[str(row['author_thread_id'])]
        result.append({
            'detail': {
                'title': title, 'unread_count': row['unread_count'], 'newest_at': row['newest_at'],
                'destination': {'pane': 'document-replies', 'id': row['artifact_id'], 'path': path, 'title': title},
                'source': {'thread_id': root or row['author_thread_id'],
                           'thread_label': (titles[root] if root else row['thread_label']) or 'Agent'},
            },
        })
    return result
