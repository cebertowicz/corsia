"""Prepare feedback text and diff payloads before a publication transaction."""
from __future__ import annotations

from pathlib import Path

from doceditor.delivery_preference import lane_for_edit
from doceditor.feedback_edits import _bounded_text, _sha256
from registry.shared_edit_classification import capped_unified_diff, combine_capped_diffs


class FeedbackPreparationChanged(RuntimeError):
    """The coalescing batch changed after preparation; retry the publication."""


def prepare_edit(db, *, artifact_id: str, recipient_id: str, author_kind: str,
                 author_id: str, actor_display: str, path: Path, before: bytes, after: bytes) -> dict:
    """Snapshot the open edit in the lane the save will use and compute all content-sized work outside BEGIN."""
    batch = db.execute(
        "SELECT id FROM doc_feedback_batches WHERE author_kind=? AND author_id=?"
        " AND artifact_id=? AND recipient_id=? AND delivery_policy=? AND state='open'",
        (author_kind, author_id, artifact_id, recipient_id, lane_for_edit(db, author_kind, recipient_id)),
    ).fetchone()
    row = db.execute("SELECT * FROM doc_feedback_edit_items WHERE batch_id=?", (batch[0],)).fetchone() if batch else None
    prior = dict(row) if row else None
    combined = None
    if prior is not None and _sha256(before) != prior["latest_sha256"]:
        closed = capped_unified_diff(path, prior["baseline_text"].encode("utf-8"),
                                     prior["latest_text"].encode("utf-8"))
        combined = combine_capped_diffs(prior["prior_segments_diff"], closed)
    return {"row": prior, "baseline_text": _bounded_text(before, "baseline_text"),
            "latest_text": _bounded_text(after, "latest_text"),
            "display": _bounded_text(actor_display.strip() or "The user", "actor_display"),
            "before_sha": _sha256(before), "after_sha": _sha256(after), "combined": combined}
