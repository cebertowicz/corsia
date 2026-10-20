"""The single review-start intent seam, pending Plans' read-only intent_of API."""

from __future__ import annotations


def requesting_node_id(db, actor) -> str | None:
    if actor.kind != "agent":
        return None
    row = db.execute(
        "SELECT node_id FROM plan_node_attempts "
        "WHERE executor_thread_id=? AND released_at IS NULL ORDER BY selected_at DESC LIMIT 1",
        (actor.id,),
    ).fetchone()
    return str(row["node_id"]) if row is not None else None


def review_intent(db, row) -> str:
    """Use the request's question until plans.public.intent_of exists upstream."""
    return row["note"] or "Review the document for accuracy, scope and actionable issues."
