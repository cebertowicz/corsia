"""Retiring and reviving a temporary worker: the plan's settle and a delegator's stop."""
from __future__ import annotations

from datetime import datetime, timezone

from core.thread_lifecycle_plans import withdraw_owned_decisions
from infra.constants import STOPPED_BY_DELEGATOR_REASON

SETTLED_WORKER_REASON = "assignment_settled"
#: A settled or delegator-stopped worker is retired; only a new assignment brings it back.
RETIRED_WORKER_REASONS = (SETTLED_WORKER_REASON, STOPPED_BY_DELEGATOR_REASON)


def _timestamp(now: datetime | str | None) -> str:
    if isinstance(now, str):
        return now
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _retire_worker(db, sid: str, *, archived_reason: str, withdraw_reason: str, now: datetime | str | None) -> bool:
    """Archive a spawned, non-standing worker and withdraw its open decisions.

    A root thread or a durable roster owner (``agent_enabled``) is never
    retired this way. Like a user archive this changes lifecycle state only;
    retention later trims unregistered stale ``working/`` units after the
    normal TTL, and registered units stay until purge. Returns whether it changed.
    """
    from registry.ownership import rehome_on_thread_end

    timestamp = _timestamp(now)
    cursor = db.execute(
        "UPDATE threads SET lifecycle_state = 'archived', archived_at = ?, "
        "archived_reason = ?, updated_at = ? "
        "WHERE id = ? AND spawned_by IS NOT NULL AND COALESCE(agent_enabled, 0) = 0 "
        "AND COALESCE(lifecycle_state, 'active') IN ('active', 'dormant')",
        (timestamp, archived_reason, timestamp, sid),
    )
    if not cursor.rowcount:
        return False
    rehome_on_thread_end(db, sid, "archived")
    withdraw_owned_decisions(db, sid, reason=withdraw_reason)
    return True


def archive_settled_worker(db, sid: str, *, now: datetime | str | None = None) -> bool:
    """Archive a temporary worker whose assignment settled; the sweep owns cleanup.

    The third synchronous archive reason after the user's and the engine's:
    the plan's, for a spawned worker with nothing left to do for the step that
    spawned it. This transition deletes no result.
    """
    return _retire_worker(
        db, sid, archived_reason=SETTLED_WORKER_REASON, withdraw_reason="assignment settled", now=now,
    )


def archive_stopped_worker(db, sid: str, *, why: str | None = None, now: datetime | str | None = None) -> bool:
    """Archive a temporary worker its delegator stopped (``delegate_stop``).

    ``why`` is the delegator's stated reason; it is recorded on every decision
    the worker had open, which this withdraws.
    """
    withdraw_reason = f"stopped by its delegator: {why}" if why else "stopped by its delegator"
    return _retire_worker(
        db, sid, archived_reason=STOPPED_BY_DELEGATOR_REASON, withdraw_reason=withdraw_reason, now=now,
    )


def reactivate_retired_worker(db, sid: str, *, now: datetime | str | None = None) -> bool:
    """Bring a settled or stopped worker back for an explicit new assignment -- only those reasons."""
    cursor = db.execute(
        "UPDATE threads SET lifecycle_state = 'active', dormant_at = NULL, "
        "archived_at = NULL, archived_reason = NULL, updated_at = ? "
        "WHERE id = ? AND lifecycle_state = 'archived' AND archived_reason IN (?, ?)",
        (_timestamp(now), sid, *RETIRED_WORKER_REASONS),
    )
    return bool(cursor.rowcount)
