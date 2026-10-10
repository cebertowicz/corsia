"""A blocker no one but the user clears is a hold; every other one is a watch.

``block_node`` names who clears the blocker. A user-owned blocker stays a
hold, reads red, and waits for the user. A blocker a thread, the system or an
outside party clears is a wait, and a wait with no end is how a plan came to
sit idle with nothing ever looking at it again (stuck plans, option C,
``shared/planning/plans-stuck-plans-2026-09-24.md``). So it arms a deadline
watch instead: the one mechanism that already ends by itself and wakes its
owner. The blocker's reason, key and owner ride in the ``watch_armed``
payload, the watch primitive untouched, and the row reads them back
(``node_waits.armed_watches``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import plans.service as service

#: Owners whose blocker is a wait rather than a hold.
WAIT_OWNERS = frozenset({"thread", "external", "system", "parent"})
DEFAULT_WAIT = timedelta(hours=24)
LONGEST_WAIT = timedelta(days=7)


def is_wait(operation: str, params) -> bool:
    """Whether this park operation is a blocker that arms a watch."""
    return operation == "block_node" and params.get("owner_kind") in WAIT_OWNERS


def resolve_parent(db, plan, params) -> None:
    """Resolve the worker shorthand ``parent`` to its delegating thread.

    The persisted watch remains an ordinary thread-owned blocker. ``to_parent``
    is stored only in its arm event, the source for every parent-side read.
    """
    if params.get("owner_kind") != "parent":
        return
    parent = plan["parent_plan_id"]
    row = parent and db.execute(
        "SELECT owner_kind,owner_id FROM plans WHERE id=?", (parent,),
    ).fetchone()
    worker = db.execute(
        "SELECT spawned_by FROM threads WHERE id=?", (plan["owner_id"],),
    ).fetchone()
    if (
        row is None or row["owner_kind"] != "thread" or worker is None
        or worker["spawned_by"] != row["owner_id"]
    ):
        raise service.PlanInputError(
            "owner_kind parent is available only in a delegated plan; block on "
            "owner_kind user or raise a decision instead"
        )
    params["owner_kind"] = "thread"
    params["owner_id"] = str(row["owner_id"])
    params["to_parent"] = True


OWNER_ONLY_REFUSAL = (
    "a blocker for anyone but the user is a wait with a deadline, and only the "
    "agent that owns this plan arms one. Block on the user (owner_kind user) "
    "or ask that agent."
)


def authorize(plan, actor) -> None:
    """Refuse a blocker wait from anyone but the plan's owner thread.

    A wait wakes that thread, so only it arms one; the user's own call is
    refused here, in these words, before the watch check refuses it in its own.
    """
    if actor.kind != "thread" or plan["owner_id"] != actor.thread_id:
        raise PermissionError(OWNER_ONLY_REFUSAL)


def watch_params(params, *, now: datetime | None = None) -> dict:
    """Return the ``arm_watch`` parameters for a non-user blocker.

    The deadline is the caller's ``condition`` when it names one, else 24
    hours out, and never more than 7 days out: a wait longer than that is
    parked work, which is the thing this replaces.
    """
    from registry.attention import AttentionError, validate_defer_condition

    now = now or datetime.now(timezone.utc)
    owner_id = params.get("owner_id")
    blocked = {
        "reason_key": service._required_text(params.get("reason_key"), "hold reason key"),
        "reason": service._required_text(params.get("reason"), "hold reason"),
        "owner_kind": params["owner_kind"],
        "owner_id": None if owner_id is None else service._required_text(
            owner_id, "hold owner id",
        ),
        "to_parent": bool(params.get("to_parent")),
    }
    condition = params.get("condition") or {
        "type": "deadline", "value": (now + DEFAULT_WAIT).isoformat(),
    }
    if not isinstance(condition, dict) or condition.get("type") != "deadline":
        raise service.PlanInputError(
            "a blocker only the user can clear is a hold; any other is a wait "
            "that ends at a deadline, so its condition must be a deadline"
        )
    try:
        condition = validate_defer_condition(condition)
    except AttentionError as exc:
        raise service.PlanInputError(str(exc)) from exc
    if datetime.fromisoformat(condition["value"]) > now + LONGEST_WAIT:
        raise service.PlanInputError(
            "a blocked wait ends within 7 days; name an earlier deadline, "
            "and call block_node again then if it still holds"
        )
    return {
        "node_id": params["node_id"],
        "expected_revision": params["expected_revision"],
        "condition": condition,
        "blocked": blocked,
    }


def watch_reason(row) -> str:
    """Name what an armed watch waits for, the row's one line.

    A blocker armed as a wait leads with its deadline, to the minute, then
    its reason: rows are cut to a few dozen bytes, and when the wait ends is
    what the reader acts on. The row's own condition already says Waiting. A
    scheduled task (``plans.scheduled_wait``) reads the same way with the
    reason it was set aside. An event wait (``plans.event_wait``) names its
    event and the deadline that ends it if the event never comes.
    """
    value = row["condition_value"]
    if row["condition_type"] != "deadline":
        return f"Waiting for {value}"
    reason = row["blocked_reason"] or row["scheduled_reason"]
    if not reason and row["artifact_integration"] and row["expected_event"]:
        return f"Waiting for {row['expected_event']} until {value}"
    if not reason:
        return f"Waiting until {value}"
    until = datetime.fromisoformat(value).astimezone(timezone.utc)
    return f"Until {until:%Y-%m-%dT%H:%MZ} · {reason}"


def explain(watch) -> str:
    """Tell the agent what its blocker became and how the wait ends."""
    return (
        "Recorded as a wait, not a blocker: only a blocker the user must clear "
        f"is a hold. Watch {watch.id} wakes you at {watch.condition['value']} "
        "to resolve the task, renew the wait (block_node again with the same "
        "reason; your parent is not woken again), raise a decision, or close it. "
        "If it clears sooner, call resolve_watch with this watch_id."
    )


def convert_hold(db, hold, *, deadline: str, provenance: str) -> None:
    """Replace one open non-user blocker hold with a wait due at ``deadline``.

    For the schema step that moves blockers placed before waits existed
    (stuck plans R4). It writes through the runtime's own writers: the hold
    clears as any clear does, and the wait is armed and recorded as
    ``block_node`` now arms one, with the hold's reason, key and owner. Both
    events carry the schema step's provenance, so a worker's parent hears
    nothing (``plan_events.from_schema_step``). The wait wakes the plan's owner thread. Nothing is
    reconciled: a held task becomes a waiting one, and neither is selectable.
    """
    from automation import watches
    from plans import holds, watch_operations

    node_id = str(hold["node_id"])
    holds.clear_write(
        node_id, str(hold["id"]), expected_kind="blocked", actor_id=None,
        provenance=provenance, db=db,
    )
    plan = db.execute(
        """SELECT p.id,p.owner_kind,p.owner_id FROM plans p
           JOIN plan_nodes n ON n.plan_id=p.id WHERE n.id=?""",
        (node_id,),
    ).fetchone()
    watch = watches.arm_watch(
        node_id, {"type": "deadline", "value": deadline},
        owner_thread=plan["owner_id"] if plan["owner_kind"] == "thread" else None,
        plan_id=str(plan["id"]), plan_node_id=node_id, db=db,
    )
    watch_operations.record_armed(
        db, node_id, watch, actor_id=None, provenance=provenance,
        blocked={
            "reason_key": hold["reason_key"], "reason": hold["reason"],
            "owner_kind": hold["owner_kind"], "owner_id": hold["owner_id"],
        },
    )
