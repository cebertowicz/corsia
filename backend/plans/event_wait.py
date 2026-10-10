"""Every event wait ends: 24 hours by default, 7 days at most (W4).

``shared/planning/plans-work-never-stops-2026-09-28.md``. A task that waits on
an artifact's event used to wait on the event alone, so an event that never
came left the task parked with nothing that would ever look at it again.

An event wait is armed as a deadline watch that keeps its artifact and the
event that ends it: the event still ends it the moment it arrives
(``plans.watch_resolution``), and the deadline is the one mechanism that ends
a wait by itself and wakes its owner. No deadline named means 24 hours; one
past 7 days is cut to 7 days, and the reply says so. A scheduled task
(``plans.scheduled_wait``) is a date the agent chose and keeps any distance; a
blocker wait (``plans.blocked_wait``) refuses a date past 7 days itself.

When the deadline passes, the ordinary watch-expiry wake says the event did
not arrive and what to do (:func:`wake_summary`).
"""

from __future__ import annotations

from datetime import datetime, timezone

from plans.blocked_wait import DEFAULT_WAIT, LONGEST_WAIT

#: Why a wait's deadline was not the one asked for, as the reply says it.
DEFAULTED = "no deadline was named; the default is 24 hours"
CAPPED = "a wait ends within 7 days, so its deadline was cut to 7 days"


def is_event_wait(params) -> bool:
    """Whether these ``arm_watch`` parameters are a plain event wait.

    A scheduled task and a blocker carry their own record and their own
    rule for how far out they may be.
    """
    return not params.get("scheduled") and not params.get("blocked")


def with_deadline(params, *, db, owner: str | None, now: datetime | None = None) -> dict:
    """Return ``params`` with the wait's condition a deadline.

    An event condition becomes a deadline 24 hours out, unless this thread
    already waits on the same event for the task: arming the same wait again
    is that wait, so it keeps its deadline and stays one watch.
    """
    now = now or datetime.now(timezone.utc)
    condition = params.get("condition")
    if not isinstance(condition, dict):
        return params
    if condition.get("type") != "deadline":
        if params.get("artifact_id") is None:
            return params  # nothing could end it; arming refuses it (#1707)
        armed = db.execute(
            """SELECT condition_value FROM expected_event_watches
               WHERE plan_node_id=? AND owner_thread_id IS ? AND expected_event IS ?
                 AND status='armed' AND condition_type='deadline'
               ORDER BY armed_at LIMIT 1""",
            (params.get("node_id"), owner, params.get("expected_event")),
        ).fetchone()
        value = armed[0] if armed else (now + DEFAULT_WAIT).isoformat()
        return {**params, "condition": {"type": "deadline", "value": value}}
    due = _instant(condition.get("value"))
    if due is not None and due > now + LONGEST_WAIT:
        capped = {"type": "deadline", "value": (now + LONGEST_WAIT).isoformat()}
        return {**params, "condition": capped}
    return params


def _instant(value) -> datetime | None:
    """A deadline as an aware instant, or None; the one validator refuses a bad one."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def note(asked, watch) -> str | None:
    """Why the armed deadline is not the one asked for, or None if it is."""
    if not isinstance(asked, dict) or asked.get("type") != "deadline":
        return DEFAULTED
    due, armed = _instant(asked.get("value")), _instant(watch.condition["value"])
    return CAPPED if due is not None and armed is not None and armed < due else None


def explain(watch, asked) -> str:
    """Tell the agent when its event wait ends and how to end it sooner.

    ``asked`` is the condition the caller passed, so the reply can say why
    the deadline differs from it.
    """
    event = watch.expected_event or "the event"
    why = note(asked, watch)
    said = f" ({why})" if why else ""
    return (
        f"Waiting for {event} until {watch.condition['value']}{said}. If it has "
        f"not arrived by then, watch {watch.id} wakes you to check its source. "
        "To end the wait sooner, call resolve_watch with this watch_id."
    )


def wake_summary(db, watch) -> str | None:
    """The expiry wake for an event wait, or None for any other watch.

    Deterministic from the watch alone, so a retried delivery carries the
    same text. A scheduled task has its own wake, and a blocker wait and a
    plain deadline name no event.
    """
    from plans.scheduled_wait import SCHEDULED_EVENT

    event = watch.expected_event
    if watch.plan_node_id is None or not event or event == SCHEDULED_EVENT:
        return None
    row = db.execute(
        "SELECT title FROM plan_nodes WHERE id=?", (watch.plan_node_id,),
    ).fetchone()
    if row is None:
        return None
    return (
        f'"{row["title"]}" (task {watch.plan_node_id}) waited for {event}, and it '
        f"did not arrive by {watch.condition['value']}. Check its source. If it is "
        "still coming, arm_watch again with a deadline for when you would act; if "
        "it is not, close_node the task or raise a decision."
    )


#: The node event that records a deadline given to an armed event wait.
DEADLINE_GIVEN = "watch_deadline_given"


def give_deadline(db, watch_row, *, deadline: str, provenance: str) -> None:
    """Give one armed event wait a deadline, in place.

    For the schema step that moves event waits armed before every wait had a
    deadline (W4). The watch keeps its id, artifact and event, so an agent
    holding the id can still resolve it and the event still ends it. Written
    under the step's ``migration.*`` provenance, the node event tells no one
    (``plans.plan_events.from_schema_step``).
    """
    from automation import watch_deadline

    watch = watch_deadline.set_deadline(db, str(watch_row["id"]), deadline)
    if watch is None:
        return
    from plans import service

    service._event(
        db, str(watch_row["plan_node_id"]), DEADLINE_GIVEN, actor_id=None,
        provenance=provenance,
        payload={"watch_id": watch.id, "condition": watch.condition},
    )
