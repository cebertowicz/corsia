"""Re-project one existing Focus run stub after a Turn in it settles.

The client asks for this when a Turn ends inside a stub it already drew, so a
comment-only Turn's work stops counting toward that stub without a page reload.
The walk is the Focus page's own, bounded to the stub's range.
"""

from __future__ import annotations

from api.thread_focus_page import (
    FOCUS_SCAN_CAP,
    InvertedRange,
    _authorized,
    _display_anchor_seq,
    _walk,
)
from history.projection import in_read_snapshot


class RangeTooLarge(ValueError):
    """The requested projection exceeds the existing Focus scan cap."""


def page_run_projection(db, thread_id: str, from_id: str, to_id: str) -> list[dict]:
    """Recompute an existing stub's bounded range after a Turn settles."""
    low = _display_anchor_seq(db, thread_id, from_id)
    high = _display_anchor_seq(db, thread_id, to_id)
    if low > high:
        raise InvertedRange(f"from_id {from_id} is newer than to_id {to_id}")
    walk = _walk(db, thread_id, high + 1, FOCUS_SCAN_CAP + 1, FOCUS_SCAN_CAP, range_floor=low)
    if walk.has_more:
        raise RangeTooLarge("Focus run range exceeds the scan cap")
    return walk.runs


def _projection_read(thread_id, from_id, to_id, *, db, user_id=None):
    if not _authorized(db, thread_id, user_id):
        return None
    return {"messages": [], "runs": page_run_projection(db, thread_id, from_id, to_id)}


def read_authorized_run_projection(db, thread_id: str, from_id: str, to_id: str, *, user_id=None):
    """Authorization and stub reprojection share one snapshot."""
    return in_read_snapshot(db, _projection_read, thread_id, from_id, to_id, user_id=user_id)
