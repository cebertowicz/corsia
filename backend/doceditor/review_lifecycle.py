"""One writer for a document review request's explicit lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone

FALLBACK_STATES = frozenset({"handed_back", "failed"})
PENDING_STATES = frozenset({"forked", *FALLBACK_STATES})


def transition(db, request_id: str, state: str, *, reason: str | None = None) -> bool:
    """Move an unfinished request once; terminal submission/withdrawal wins races.

    The caller holds the request's write transaction, including any Plans or
    feedback effect. A failed fork can subsequently submit via its live task;
    its failure reason remains available after that submission.
    """
    if state not in {"forked", "submitted", "withdrawn", *FALLBACK_STATES}:
        raise ValueError(f"unknown review state: {state}")
    if state in FALLBACK_STATES and not reason:
        raise ValueError("a review hand-back requires a reason")
    source = "state='forked'" if state in FALLBACK_STATES else "state IN ('forked','handed_back','failed')"
    return bool(db.execute(
        f"UPDATE doc_review_requests SET state=?,state_changed_at=?,reason=COALESCE(?,reason) "
        f"WHERE id=? AND {source}",
        (state, datetime.now(timezone.utc).isoformat(), reason, request_id),
    ).rowcount)


def is_open(db, row) -> bool:
    """A fork is pending; a handed-back request follows its public Plans task."""
    if row["state"] == "forked":
        return True
    if row["state"] in FALLBACK_STATES:
        from plans import public

        try:
            return public.review_is_open(db, row["node_id"])
        except public.PlanError:
            return False  # A deleted continuation cannot receive document changes.
    return False
