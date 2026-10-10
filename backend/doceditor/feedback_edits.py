"""Leaf persistence for coalesced document-edit feedback items."""

from __future__ import annotations

import hashlib
from pathlib import Path

from doceditor.document_scope import MAX_TEXT_BYTES
from registry.shared_edit_classification import combine_capped_diffs, capped_unified_diff


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _bounded_text(content: bytes | str, field: str) -> str:
    raw = content if isinstance(content, bytes) else content.encode("utf-8")
    if len(raw) > MAX_TEXT_BYTES:
        raise ValueError(f"{field} exceeds the {MAX_TEXT_BYTES}-byte document limit")
    return raw.decode("utf-8", errors="replace")


def record_edit_item(
    db,
    *,
    batch_id: str,
    actor_display: str,
    path: Path,
    before: bytes,
    after: bytes,
    now: str,
    prepared: dict | None = None,
) -> int:
    """Coalesce one save into an existing canonical batch and return its item delta."""
    baseline_text = prepared["baseline_text"] if prepared else _bounded_text(before, "baseline_text")
    latest_text = prepared["latest_text"] if prepared else _bounded_text(after, "latest_text")
    display = prepared["display"] if prepared else _bounded_text(actor_display.strip() or "The user", "actor_display")
    row = db.execute(
        "SELECT * FROM doc_feedback_edit_items WHERE batch_id=?", (batch_id,)
    ).fetchone()
    if prepared is not None and (dict(row) if row else None) != prepared["row"]:
        from doceditor.feedback_edit_preparation import FeedbackPreparationChanged
        raise FeedbackPreparationChanged("The feedback batch changed during publication preparation")
    before_sha = prepared["before_sha"] if prepared else _sha256(before)
    after_sha = prepared["after_sha"] if prepared else _sha256(after)
    if row is None:
        db.execute(
            """INSERT INTO doc_feedback_edit_items
               (batch_id,actor_display,prior_segments_diff,baseline_sha256,
                baseline_text,latest_sha256,latest_text,save_count,first_at,last_at)
               VALUES (?,?,?,?,?,?,?,1,?,?)""",
            (batch_id, display, "", before_sha, baseline_text,
             after_sha, latest_text, now, now),
        )
        return 1
    prior = str(row["prior_segments_diff"])
    segment_baseline_sha = str(row["baseline_sha256"])
    segment_baseline_text = str(row["baseline_text"])
    if before_sha != str(row["latest_sha256"]):
        if prepared is not None:
            prior = prepared["combined"]
        else:
            closed = capped_unified_diff(
                path,
                segment_baseline_text.encode("utf-8"),
                str(row["latest_text"]).encode("utf-8"),
            )
            prior = combine_capped_diffs(prior, closed)
        segment_baseline_sha = before_sha
        segment_baseline_text = baseline_text
    db.execute(
        """UPDATE doc_feedback_edit_items
           SET prior_segments_diff=?,baseline_sha256=?,baseline_text=?,
               latest_sha256=?,latest_text=?,save_count=save_count+1,last_at=?
           WHERE batch_id=?""",
        (prior, segment_baseline_sha, segment_baseline_text,
         after_sha, latest_text, now, batch_id),
    )
    return 0
