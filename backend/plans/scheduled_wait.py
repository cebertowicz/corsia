"""An agent sets a task aside by scheduling it, never by pausing it.

Pause is the user's (``shared/planning/plans-work-never-stops-2026-09-28.md``,
W1). An agent that wants to come back to a task later arms a deadline watch
with a reason: ``arm_watch`` with ``reason`` and no ``artifact_id``. It may be
any distance out, because the watch ends by itself and wakes its owner, which
a pause never did. The reason rides in the ``watch_armed`` payload as
``scheduled`` beside ``blocked``; the watch primitive is
untouched, and the row reads it back (``node_waits.armed_watches``).

When the date arrives the ordinary watch-expiry wake names the task, how long
it was set aside and why (:func:`wake_summary`, W2).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json

import plans.service as service

#: What a scheduled watch waits for when the caller names nothing.
SCHEDULED_EVENT = "scheduled"
#: A whole-plan pause and a task pause: both are the user's alone.
PAUSE_OPERATIONS = frozenset({"pause", "pause_node"})

OWNER_ONLY_REFUSAL = (
    "a scheduled task wakes the agent that owns this plan, and only that agent "
    "schedules one."
)


def pause_refusal(node_id: str | None) -> str:
    """The one line an agent reads when it reaches for pause."""
    return (
        "Pause is the user's. To come back to this later, schedule it: "
        f"arm_watch(node_id={node_id or '<task id>'}, "
        "condition={type: deadline, value: <ISO time>}, reason=<why>)."
    )


def is_scheduled(operation: str, params) -> bool:
    """Whether this ``arm_watch`` sets the task aside until a date, with a reason."""
    return operation == "arm_watch" and "reason" in params


def authorize(plan, actor) -> None:
    """Refuse a schedule from anyone but the plan's owner thread."""
    if actor.kind != "thread" or plan["owner_id"] != actor.thread_id:
        raise PermissionError(OWNER_ONLY_REFUSAL)


def watch_params(params) -> dict:
    """Return the ``arm_watch`` parameters for a scheduled task.

    A schedule is a date, not an event: it names no artifact and its condition
    is a deadline. There is no upper bound on the date; the reason is required.
    """
    reason = service._required_text(params.get("reason"), "reason")
    if params.get("artifact_id") is not None:
        raise service.PlanInputError(
            "reason schedules this task until a deadline, so it takes no "
            "artifact_id. To wait for an event on the artifact, drop reason; "
            "to schedule the task, drop artifact_id"
        )
    condition = params.get("condition")
    if not isinstance(condition, dict) or condition.get("type") != "deadline":
        raise service.PlanInputError(
            "a scheduled task comes back at a date: its condition must be a deadline"
        )
    scheduled = {key: value for key, value in params.items() if key != "reason"}
    scheduled.setdefault("expected_event", SCHEDULED_EVENT)
    scheduled["scheduled"] = {"reason": reason}
    return scheduled


def require_event(params) -> None:
    """Refuse a watch that is neither a schedule nor names the event ending it."""
    event = params.get("expected_event")
    if not isinstance(event, str) or not event.strip():
        raise service.PlanInputError(
            "arm_watch needs the expected_event that ends the wait, or a reason "
            "to schedule the task until the deadline"
        )


def explain(watch) -> str:
    """Tell the agent when its scheduled task comes back."""
    return (
        f"Scheduled until {watch.condition['value']}. Watch {watch.id} wakes you "
        "then. To pick it up sooner, call resolve_watch with this watch_id."
    )


def _duration(seconds: float) -> str:
    """A span in the largest whole unit that reads naturally."""
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = hours // 24
    return f"{days} days"


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def wake_summary(db, watch) -> str | None:
    """The expiry wake for a scheduled task, or None for any other watch (W2).

    Deterministic from the watch alone (how long = deadline minus arming), so a
    retried delivery carries the same text.
    """
    if watch.plan_node_id is None or watch.condition.get("type") != "deadline":
        return None
    row = db.execute(
        """SELECT n.title, e.payload_json, w.armed_at FROM plan_node_events e
           JOIN plan_nodes n ON n.id=e.node_id
           JOIN expected_event_watches w ON w.id=?
           WHERE e.node_id=? AND e.event='watch_armed'
             AND json_extract(e.payload_json,'$.watch_id')=?
             AND json_extract(e.payload_json,'$.scheduled.reason') IS NOT NULL
           ORDER BY e.created_at DESC LIMIT 1""",
        (watch.id, watch.plan_node_id, watch.id),
    ).fetchone()
    if row is None:
        return None
    reason = json.loads(row["payload_json"])["scheduled"]["reason"]
    aside = _parse(watch.condition["value"]) - _parse(row["armed_at"])
    return (
        f'Scheduled task "{row["title"]}" is due (task {watch.plan_node_id}). '
        f"You set it aside {_duration(aside.total_seconds())} ago: {reason}. "
        "Start it now, schedule it again with a reason, close it, or raise a decision."
    )


#: The reason a migrated pause carries when its hold recorded none.
MOVED_REASON = "Set aside before agents could pause"


def is_agent_pause(hold, plan_owner_id: str | None) -> bool:
    """Whether an open hold is a pause the plan's own agent placed.

    The user's pause, however it was recorded, is the user's
    (``user_steering.is_user_pause``); only the owner thread's own pause moves.
    """
    from plans.user_steering import is_user_pause

    return (
        hold["kind"] == "paused" and not is_user_pause(hold)
        and hold["owner_kind"] == "thread" and hold["owner_id"] == plan_owner_id
    )


def schedule_task(db, node_id: str, *, reason: str, deadline: str, provenance: str) -> None:
    """Arm a scheduled wait for a schema step; its provenance tells no one."""
    from automation import watches
    from plans import watch_operations

    plan = db.execute(
        """SELECT p.id,p.owner_kind,p.owner_id FROM plans p
           JOIN plan_nodes n ON n.plan_id=p.id WHERE n.id=?""",
        (node_id,),
    ).fetchone()
    watch = watches.arm_watch(
        node_id, {"type": "deadline", "value": deadline},
        owner_thread=plan["owner_id"] if plan["owner_kind"] == "thread" else None,
        expected_event=SCHEDULED_EVENT,
        plan_id=str(plan["id"]), plan_node_id=node_id, db=db,
    )
    watch_operations.record_armed(
        db, node_id, watch, blocked=None, scheduled={"reason": reason},
        actor_id=None, provenance=provenance,
    )


def schedule_pause(db, hold, *, deadline: str, provenance: str) -> None:
    """Replace one open agent pause hold with a scheduled task due at ``deadline``.

    For the schema step that moves pauses placed before agents lost pause
    (W1). The hold clears as any clear does and the wait is armed as a
    schedule is now, with the hold's reason. The schema step's provenance
    tells no one (``plan_events.from_schema_step``).
    """
    from plans import holds

    node_id = str(hold["node_id"])
    holds.clear_write(
        node_id, str(hold["id"]), expected_kind="paused", actor_id=None,
        provenance=provenance, db=db,
    )
    schedule_task(
        db, node_id, reason=(hold["reason"] or "").strip() or MOVED_REASON,
        deadline=deadline, provenance=provenance,
    )


def lift_agent_plan_pause(db, plan_id: str, *, provenance: str) -> None:
    """Lift a whole-plan pause its own agent placed, through the runtime resume."""
    from plans import lifecycle

    plan = service._plan_row(db, plan_id)
    lifecycle.transition(
        plan_id, "active", expected_revision=int(plan["revision"]),
        actor_id=None, provenance=provenance, db=db,
    )
