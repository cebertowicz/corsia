"""Person-directed document comment counts and dismissal."""

import json

from doceditor import feedback_state

# The comments delivered to the person (``feedback_state.directed_sql``) on the indexed documents.
# Binds user, documents, the person's author id, user.
_DIRECTED = f"""FROM doc_comments item
             LEFT JOIN doc_comment_thread_views view
               ON view.user_id=? AND view.root_id=COALESCE(item.parent_id,item.id)
            WHERE item.artifact_id IN (SELECT value FROM json_each(?))
              AND {feedback_state.directed_sql('item')}"""

# Past the thread's read watermark (time, then id).
_UNREAD = """AND (item.created_at>COALESCE(view.seen_at,'')
                   OR (item.created_at=COALESCE(view.seen_at,'') AND item.id>COALESCE(view.seen_reply_id,'')))"""


def _params(artifact_ids, user_id: str) -> tuple:
    return (user_id, json.dumps(list(artifact_ids)), feedback_state.USER_AUTHOR_ID, user_id)


def counts_many(db, artifact_ids, user_id: str) -> dict[str, tuple[int, int]]:
    """Count unread comments and unread, undecided suggestions in one read."""
    if not artifact_ids:
        return {}
    rows = db.execute(
        f"""SELECT item.artifact_id, SUM(item.replacement IS NULL), SUM(item.replacement IS NOT NULL)
              {_DIRECTED} {_UNREAD}
             GROUP BY item.artifact_id""",
        _params(artifact_ids, user_id),
    ).fetchall()
    return {row[0]: (row[1], row[2]) for row in rows}


def counts(db, artifact_id: str, user_id: str) -> tuple[int, int]:
    return counts_many(db, [artifact_id], user_id).get(artifact_id, (0, 0))


def dismiss(db, artifact_id: str, user_id: str) -> None:
    """Mark directed items read without deciding suggestions."""
    latest = {}
    for row in db.execute(
        f"SELECT item.id, item.parent_id {_DIRECTED} ORDER BY item.created_at, item.id",
        _params([artifact_id], user_id),
    ).fetchall():
        latest[row['parent_id'] or row['id']] = row['id']
    for root_id, item_id in latest.items():
        feedback_state.mark_thread_viewed(db, user_id, artifact_id, root_id, item_id)
