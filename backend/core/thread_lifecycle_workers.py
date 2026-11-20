"""Retiring and reviving a temporary worker: the plan's and a delegator's archive reasons."""
from __future__ import annotations

from datetime import datetime

from infra.constants import STOPPED_BY_DELEGATOR_REASON

SETTLED_WORKER_REASON = "assignment_settled"
#: A settled or delegator-stopped worker is retired; only a new assignment brings it back.
RETIRED_WORKER_REASONS = (SETTLED_WORKER_REASON, STOPPED_BY_DELEGATOR_REASON)


def _timestamp(now: datetime | str | None) -> str:
    from core.thread_lifecycle import _iso, _now

    return now if isinstance(now, str) else _iso(now or _now())


def archive_settled_worker(
    db, sid: str, *, now: datetime | str | None = None, reason: str = SETTLED_WORKER_REASON,
) -> bool:
    """Archive a temporary worker whose assignment settled; the sweep owns cleanup.

    The third synchronous archive reason after the user's and the engine's:
    the plan's, for a spawned worker with nothing left to do for the step that
    spawned it (``reason`` is ``stopped_by_delegator`` when its delegator
    stopped it). A root thread or a durable roster owner (``agent_enabled``) is
    never retired this way. Like a user archive this changes lifecycle state
    and withdraws open decisions. Retention later trims unregistered stale
    ``working/`` units after the normal TTL; registered units stay until purge.
    This transition deletes no result. Returns whether it changed.
    """
    from core.thread_lifecycle import _rehome_on_end, _withdraw_owned_decisions

    timestamp = _timestamp(now)
    cursor = db.execute(
        "UPDATE threads SET lifecycle_state = 'archived', archived_at = ?, "
        "archived_reason = ?, updated_at = ? "
        "WHERE id = ? AND spawned_by IS NOT NULL AND COALESCE(agent_enabled, 0) = 0 "
        "AND COALESCE(lifecycle_state, 'active') IN ('active', 'dormant')",
        (timestamp, reason, timestamp, sid),
    )
    if not cursor.rowcount:
        return False
    _rehome_on_end(db, sid, "archived")
    _withdraw_owned_decisions(db, sid, reason="assignment settled")
    return True


def reactivate_settled_worker(db, sid: str, *, now: datetime | str | None = None) -> bool:
    """Bring a retired worker back for an explicit new assignment -- only those reasons."""
    cursor = db.execute(
        "UPDATE threads SET lifecycle_state = 'active', dormant_at = NULL, "
        "archived_at = NULL, archived_reason = NULL, updated_at = ? "
        "WHERE id = ? AND lifecycle_state = 'archived' AND archived_reason IN (?, ?)",
        (_timestamp(now), sid, *RETIRED_WORKER_REASONS),
    )
    return bool(cursor.rowcount)
