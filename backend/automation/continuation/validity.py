"""Final source-state validation for plan continuation FIFO admission."""

from __future__ import annotations

from typing import Any

from automation.continuation.models import StaleContinuation
from automation.continuation.sources.liveness import LivenessSource
from automation.continuation.sources.schedules import ScheduleSource
from automation.continuation.sources.watch_expiry import WatchExpirySource
from automation.continuation.sources.watchdog import WatchdogSource
from plans import public

#: Every routed continuation event, by the source that can judge it. A routed
#: event with no entry here reaches the thread with no condition, and no claim
#: ever asks again whether the work it described can still move.
_EVENT_KINDS = {
    "plan.liveness_wake": "t0_liveness",
    "plan.watchdog_wake": "t1_watchdog",
    "plan.watch_expired": "t2_watch_expiry",
    "automation.scheduled_wake": "automation_clock",
}

#: The same sources, by kind. One map, so appending and claiming can never
#: disagree about who owns a wake's rules.
_SOURCES = {
    "t0_liveness": LivenessSource,
    "t1_watchdog": WatchdogSource,
    "t2_watch_expiry": WatchExpirySource,
    "automation_clock": ScheduleSource,
}


def condition_for_event(
    internal_event: str,
    details: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the exact plan-wake condition carried to FIFO admission."""

    kind = _EVENT_KINDS.get(internal_event)
    if kind is None:
        return None
    source = dict(details or {})
    return {
        "kind": kind,
        "subject_id": source.get("subject_id"),
        "transition": source.get("source_transition"),
        "details": source,
    }


def require_current(
    thread_id: str,
    condition: dict[str, Any],
    *,
    db,
) -> None:
    """Refuse a plan wake that changed after collection but before append."""

    if not is_current(thread_id, condition, db=db):
        raise StaleContinuation("plan continuation source is no longer current")


def is_claimable(
    thread_id: str,
    condition: dict[str, Any],
    *,
    db,
) -> bool:
    """Return whether a queued wake still has work for the Turn that would claim it.

    Deliberately not ``is_current``. Appending asks whether the source row is
    still the one the wake was collected from; claiming asks only whether the
    work it described can still move. Delivery happens between the two and
    settles the source -- the T0 row re-arms at its next cycle, the watchdog
    records its audit -- so re-asking the first question here would cancel a
    wake for the crime of having been delivered.
    """
    kind = condition.get("kind")
    if kind in public.WAKE_KINDS:
        # A result-bearing wake's rule already asks only about the work, and a
        # pause's only about the Turn it steers, so one predicate answers both.
        return public.wake_is_current(thread_id, condition, db=db)
    source = _SOURCES.get(kind) if isinstance(kind, str) else None
    details = condition.get("details")
    if source is None or not isinstance(details, dict):
        return False
    return source().still_actionable(thread_id, details, db=db)


def is_current(
    thread_id: str,
    condition: dict[str, Any],
    *,
    db,
) -> bool:
    """Return whether the source row is still the one this wake was collected from.

    The append-time question. A wake collected seconds ago can already describe
    work that was closed, released or made to wait, and appending it then would
    queue a Turn for nothing. What a *claim* asks is narrower — see
    ``is_claimable`` — because delivery settles the source in between.
    """

    kind = condition.get("kind")
    if kind in public.WAKE_KINDS:
        # A result-bearing wake carries its own rule: Plans owns what "still
        # worth a Turn" means for the thread it names, at both moments.
        return public.wake_is_current(thread_id, condition, db=db)
    source = _SOURCES.get(kind) if isinstance(kind, str) else None
    details = condition.get("details")
    return not (
        source is None
        or not isinstance(details, dict)
        or details.get("trigger_kind") != kind
        or not source().is_current(
            thread_id,
            condition.get("subject_id"),
            condition.get("transition"),
            details,
            db=db,
        )
    )
