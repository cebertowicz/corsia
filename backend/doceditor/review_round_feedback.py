"""The rows and counts of user feedback in a document review round."""

from doceditor.feedback_state import USER_AUTHOR_ID


def round_feedback(db, artifact_id: str, since: str | None) -> tuple[dict, dict]:
    """Return counts and IDs from the same rows; delivered edit counts may exceed one per batch ID."""
    floor = since or ""
    comments: list[str] = []
    suggestions: list[str] = []
    for row in db.execute(
        "SELECT id, replacement FROM doc_comments "
        "WHERE artifact_id=? AND author_kind='user' AND created_at>? ORDER BY created_at,id",
        (artifact_id, floor),
    ):
        (comments if row["replacement"] is None else suggestions).append(row["id"])
    open_edits = list(db.execute(
        """SELECT b.id FROM doc_feedback_edit_items e
             JOIN doc_feedback_batches b ON b.id=e.batch_id
            WHERE b.state='open' AND b.author_kind='user' AND b.author_id=?
              AND b.artifact_id=? AND e.last_at>?
              AND (e.prior_segments_diff<>'' OR e.latest_sha256<>e.baseline_sha256)""",
        (USER_AUTHOR_ID, artifact_id, floor),
    ))
    delivered = list(db.execute(
        """SELECT b.id, MAX(b.item_count - (
                    SELECT COUNT(*) FROM doc_feedback_batch_items i WHERE i.batch_id=b.id), 0) AS edits
             FROM doc_feedback_batches b
            WHERE b.state='delivered' AND b.author_kind='user' AND b.author_id=?
              AND b.artifact_id=? AND b.last_item_at>?""",
        (USER_AUTHOR_ID, artifact_id, floor),
    ))
    edit_ids = sorted({row["id"] for row in open_edits} | {row["id"] for row in delivered if row["edits"]})
    counts = {"comments": len(comments), "suggestions": len(suggestions),
              "edits": len(open_edits) + sum(row["edits"] for row in delivered)}
    return counts, {"comments": comments, "suggestions": suggestions, "edits": edit_ids}
