"""Person-directed document comment counts and dismissal."""

import json

from doceditor import feedback_state


def _items_many(db, artifact_ids, user_id: str):
    """The comments delivered to the person by the canonical recipients writer."""
    if not artifact_ids:
        return []
    # Start with the indexed documents, not every UI feedback batch in the workspace.
    return db.execute(
        """SELECT DISTINCT item.artifact_id, item.id, item.parent_id, item.created_at,
                  item.replacement, item.decision, view.seen_at, view.seen_reply_id
             FROM doc_comments item
             CROSS JOIN doc_feedback_batch_items batch_item ON batch_item.comment_id=item.id
             CROSS JOIN doc_feedback_batches batch ON batch.id=batch_item.batch_id
             JOIN threads author ON author.id=item.author_thread_id
             LEFT JOIN doc_comment_thread_views view
               ON view.user_id=? AND view.root_id=COALESCE(item.parent_id,item.id)
            WHERE item.artifact_id IN (SELECT value FROM json_each(?))
              AND batch.artifact_id=item.artifact_id AND batch.recipient_id='ui'
              AND item.author_kind='agent' AND author.user_id=?
            ORDER BY item.created_at,item.id""",
        (user_id, json.dumps(list(artifact_ids)), user_id),
    ).fetchall()


def _items(db, artifact_id: str, user_id: str):
    return _items_many(db, [artifact_id], user_id)


def unread_items(db, artifact_id: str, user_id: str):
    """The directed items whose read watermark and decision still need attention."""
    return [row for row in _items(db, artifact_id, user_id)
            if (row['created_at'], row['id']) > (row['seen_at'] or '', row['seen_reply_id'] or '')
            and (row['replacement'] is None or row['decision'] is None)]


def counts_many(db, artifact_ids, user_id: str) -> dict[str, tuple[int, int]]:
    """Count unread comments and unread, undecided suggestions in one read."""
    counts: dict[str, tuple[int, int]] = {}
    for row in _items_many(db, artifact_ids, user_id):
        if (row['created_at'], row['id']) <= (row['seen_at'] or '', row['seen_reply_id'] or ''):
            continue
        if row['replacement'] is not None and row['decision'] is not None:
            continue
        comments, suggestions = counts.get(row['artifact_id'], (0, 0))
        counts[row['artifact_id']] = (comments + (row['replacement'] is None),
                                      suggestions + (row['replacement'] is not None))
    return counts


def counts(db, artifact_id: str, user_id: str) -> tuple[int, int]:
    return counts_many(db, [artifact_id], user_id).get(artifact_id, (0, 0))


def dismiss(db, artifact_id: str, user_id: str) -> None:
    """Mark directed items read without deciding suggestions."""
    latest = {}
    for row in _items(db, artifact_id, user_id):
        latest[row['parent_id'] or row['id']] = row['id']
    for root_id, item_id in latest.items():
        feedback_state.mark_thread_viewed(db, user_id, artifact_id, root_id, item_id)
