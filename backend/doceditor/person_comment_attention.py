"""Person-directed document comment counts and dismissal."""

import json

from doceditor import feedback_state

# The comments delivered to the person by the canonical recipients writer. It starts from the
# indexed documents, not every UI feedback batch in the workspace. Binds user, documents, user.
_DIRECTED = """FROM doc_comments item
             CROSS JOIN doc_feedback_batch_items batch_item ON batch_item.comment_id=item.id
             CROSS JOIN doc_feedback_batches batch ON batch.id=batch_item.batch_id
             JOIN threads author ON author.id=item.author_thread_id
             LEFT JOIN doc_comment_thread_views view
               ON view.user_id=? AND view.root_id=COALESCE(item.parent_id,item.id)
            WHERE item.artifact_id IN (SELECT value FROM json_each(?))
              AND batch.artifact_id=item.artifact_id AND batch.recipient_id='ui'
              AND item.author_kind='agent' AND author.user_id=?"""

# Past the thread's read watermark (time, then id), and a suggestion not yet decided.
_UNREAD = """AND (item.created_at>COALESCE(view.seen_at,'')
                   OR (item.created_at=COALESCE(view.seen_at,'') AND item.id>COALESCE(view.seen_reply_id,'')))
              AND (item.replacement IS NULL OR item.decision IS NULL)"""


def _params(artifact_ids, user_id: str) -> tuple:
    return (user_id, json.dumps(list(artifact_ids)), user_id)


def unread_rows(artifact_ids, user_id: str) -> tuple[str, tuple]:
    """The directed items still needing attention, as a FROM clause and its parameters."""
    return f"{_DIRECTED} {_UNREAD}", _params(artifact_ids, user_id)


def counts_many(db, artifact_ids, user_id: str) -> dict[str, tuple[int, int]]:
    """Count unread comments and unread, undecided suggestions in one read."""
    if not artifact_ids:
        return {}
    sql, params = unread_rows(artifact_ids, user_id)
    rows = db.execute(
        f"""SELECT item.artifact_id, SUM(item.replacement IS NULL), SUM(item.replacement IS NOT NULL)
              FROM (SELECT DISTINCT item.artifact_id, item.id, item.replacement {sql}) item
             GROUP BY item.artifact_id""",
        params,
    ).fetchall()
    return {row[0]: (row[1], row[2]) for row in rows}


def counts(db, artifact_id: str, user_id: str) -> tuple[int, int]:
    return counts_many(db, [artifact_id], user_id).get(artifact_id, (0, 0))


def dismiss(db, artifact_id: str, user_id: str) -> None:
    """Mark directed items read without deciding suggestions."""
    latest = {}
    for row in db.execute(
        f"SELECT DISTINCT item.id, item.parent_id, item.created_at {_DIRECTED} ORDER BY item.created_at, item.id",
        _params([artifact_id], user_id),
    ).fetchall():
        latest[row['parent_id'] or row['id']] = row['id']
    for root_id, item_id in latest.items():
        feedback_state.mark_thread_viewed(db, user_id, artifact_id, root_id, item_id)
