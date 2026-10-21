"""Bounded synchronous source capture for a disposable review pass."""

from __future__ import annotations

from dataclasses import replace

from core.artifact_review_capture import ReviewForkFailure
from history.projection import in_read_snapshot, render_portable_history
from history.types import HistoryBoundary

# A review pass is an optimization over the ordinary continuation task. Refuse
# its synchronous capture above this source-row budget rather than truncate an
# exact tail or make request creation grow with an unbounded conversation.
# Include covered rows: scoped history validates the anchor's owning segment,
# which can begin arbitrarily far before the installed checkpoint.
REVIEW_TAIL_MAX_ROWS = 1024


def _bounded_history(thread_id, previous, *, db):
    start = HistoryBoundary.of_checkpoint(previous) if previous else None
    rows = db.execute(
        "SELECT seq FROM messages WHERE thread_id=? AND delivery_state='delivered' "
        "ORDER BY seq LIMIT ?",
        (thread_id, REVIEW_TAIL_MAX_ROWS + 1),
    ).fetchall()
    if len(rows) > REVIEW_TAIL_MAX_ROWS:
        raise ReviewForkFailure("history_tail_bound")
    projection = render_portable_history(thread_id, db=db, start_exclusive=start)
    if previous is not None:
        from infra.checkpoint_receipts import frozen_presence_line

        projection = replace(projection, resume_presence_line=frozen_presence_line(db, previous.id))
    return projection


def bounded_review_history(thread_id, previous, *, db):
    """Render a tail only when its full validation source has at most 1,024 rows.

    The guard fetches at most 1,025 integer rows, never message payloads. It
    includes covered rows to bound anchor-segment validation as well as the
    tail. Above the cap the request follows its failure-to-continuation path.
    """
    return in_read_snapshot(db, _bounded_history, thread_id, previous)
