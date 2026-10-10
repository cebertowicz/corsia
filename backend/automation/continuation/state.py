"""Current durable admission state for the continuation runner."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from automation.continuation import admission

COST_WINDOW_SECONDS = 3600


def parse_iso(value: str) -> datetime:
    """Parse one runner timestamp as an aware datetime."""

    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _cost_for_scope(db, scope_id: str, since: str) -> float:
    if scope_id == "__workspace__":
        row = db.execute(
            """SELECT COALESCE(SUM(u.cost_usd), 0.0) AS cost
               FROM llm_usage u JOIN threads s ON s.id=u.thread_id
               WHERE s.project_id IS NULL AND u.created_at>=?""",
            (since,),
        ).fetchone()
    else:
        row = db.execute(
            """SELECT COALESCE(SUM(u.cost_usd), 0.0) AS cost
               FROM llm_usage u JOIN threads s ON s.id=u.thread_id
               WHERE s.project_id=? AND u.created_at>=?""",
            (scope_id, since),
        ).fetchone()
    return float(row["cost"])


def with_startup_jitter(request, now: str, maximum: int):
    """Apply deterministic per-thread startup spread to an unbounded request."""

    if maximum <= 0 or request.not_before is not None:
        return request
    seconds = admission.restart_jitter_seconds(
        request.thread_id,
        maximum=maximum,
    )
    due = parse_iso(now) + timedelta(seconds=seconds)
    return replace(request, not_before=due.isoformat())


def admission_snapshot(db, batch, now: datetime) -> admission.AdmissionSnapshot:
    """Read cost, fairness, and global in-flight state for one sweep."""

    scopes = {trigger.scope_id for trigger in batch}
    since = (now - timedelta(seconds=COST_WINDOW_SECONDS)).isoformat()
    costs = {scope: _cost_for_scope(db, scope, since) for scope in scopes}
    dispatches = {
        row["scope_id"]: row["last_dispatch"]
        for row in db.execute(
            """SELECT scope_id, MAX(dispatched_at) AS last_dispatch
               FROM continuation_triggers
               WHERE dispatched_at IS NOT NULL GROUP BY scope_id"""
        ).fetchall()
    }
    in_flight = db.execute(
        "SELECT COUNT(*) FROM continuation_triggers WHERE state='dispatched'"
    ).fetchone()[0]
    return admission.AdmissionSnapshot(int(in_flight), costs, dispatches)
