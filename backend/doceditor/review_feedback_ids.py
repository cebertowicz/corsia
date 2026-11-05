"""Identifiers for the same user feedback included in review-round counts."""

from doceditor.feedback_state import USER_AUTHOR_ID


def round_feedback_ids(db, artifact_id: str, since: str | None) -> dict:
    """Return comment and suggestion row IDs and edit feedback batch IDs."""
    floor = since or ""
    comments: list[str] = []
    suggestions: list[str] = []
    for row in db.execute(
        "SELECT id, replacement FROM doc_comments "
        "WHERE artifact_id=? AND author_kind='user' AND created_at>? ORDER BY created_at,id",
        (artifact_id, floor),
    ):
        (comments if row["replacement"] is None else suggestions).append(row["id"])
    edits = [row["id"] for row in db.execute(
        """SELECT b.id FROM doc_feedback_batches b
             JOIN doc_feedback_edit_items e ON e.batch_id=b.id
            WHERE b.state='open' AND b.author_kind='user' AND b.author_id=?
              AND b.artifact_id=? AND e.last_at>?
              AND (e.prior_segments_diff<>'' OR e.latest_sha256<>e.baseline_sha256)
            UNION ALL
           SELECT b.id FROM doc_feedback_batches b
            WHERE b.state='delivered' AND b.author_kind='user' AND b.author_id=?
              AND b.artifact_id=? AND b.last_item_at>?
              AND b.item_count > (SELECT COUNT(*) FROM doc_feedback_batch_items i WHERE i.batch_id=b.id)
            ORDER BY id""",
        (USER_AUTHOR_ID, artifact_id, floor, USER_AUTHOR_ID, artifact_id, floor),
    )]
    return {"comments": comments, "suggestions": suggestions, "edits": edits}
