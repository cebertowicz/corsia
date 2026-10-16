"""Slot sequencing after a plan write releases or parks a task.

Every mutation that takes a task off its executor's slot ends here: the
release, the caller's named next task, reconciliation, and the wake are
ordered once, in one place, so no operation invents its own order.
"""

from __future__ import annotations

from typing import Any

import plans.boundary_fields as fields
import plans.service as service
import plans.watch_operations as watch_operations


def _raised_in(actor) -> dict[str, str]:
    """Where this call ran: its Turn and the ids its tool call is known by."""
    found = {"turn_id": actor.turn_id, "tool_invocation_id": actor.tool_invocation_id,
             "provider_call_id": actor.provider_call_id}
    return {key: value for key, value in found.items() if value}


def after_transition(
    node_id: str, reason: str, common: dict, *, executor: str | None = None,
) -> dict:
    """Release a task's selection and admit the next eligible task without preemption.

    Not every transition through here settles the work: a rejection hands the
    same task back to the same executor. Whether that reads as a close is the
    wake writer's decision, from the task's state, not this wrapper's.

    ``executor`` is the thread acting, when one is: it is told nothing here,
    because its own result already carries these facts.
    """
    from plans import admission, plan_wake

    db = common["db"]
    admission.release(node_id, reason, db=db)
    node = service._node_row(db, node_id)
    plan = service._plan_row(db, node["plan_id"])
    admission.reconcile_plan(
        node["plan_id"], actor_id=common["actor_id"],
        provenance=common["provenance"], causing_executor=executor, db=db,
    )
    plan_wake.notify_closed(
        db, node_id, plan_id=str(node["plan_id"]),
        executor=admission.resolve_executor(db, node, plan),
        causing_executor=executor,
    )
    return admission.result(node_id, db=db)


def after_close(
    node_id: str, reason: str, params: dict[str, Any], common: dict,
    *, executor: str | None = None,
) -> dict:
    """Release a closed task, start the named next task, reconcile, and report next.

    The named start runs after the release, so what the close itself changed (a
    freed slot, a satisfied dependency) counts for it, and before
    reconciliation, so the task the caller chose is offered its executor's slot
    ahead of any already-active task reconciliation would select.
    """
    from plans import admission, close_next

    db = common["db"]
    closed = service._node_row(db, node_id)
    slot = close_next.slot_before(db, closed)
    admission.release(node_id, reason, db=db)
    named = None
    if "next_node_id" in params:
        named = close_next.start_named(
            db, fields.text(params, "next_node_id"), slot,
            actor_id=common["actor_id"], provenance=common["provenance"],
        )
    admission.reconcile_plan(
        slot.plan_id, actor_id=common["actor_id"],
        provenance=common["provenance"], causing_executor=executor, db=db,
    )
    from plans import plan_wake

    plan_wake.notify_closed(
        db, node_id, plan_id=slot.plan_id, executor=slot.executor,
        causing_executor=executor,
    )
    result = admission.result(node_id, db=db)
    result.update(close_next.next_fields(db, closed, slot, named, acting=executor))
    return result


def _park_write(plan_id: str, operation: str, params: dict[str, Any], actor, common: dict):
    """Apply one park operation's write and release; return the armed watch, if any.

    A blocker someone other than the user clears arms a deadline watch
    (``plans.blocked_wait``); only the user's own blocker is a hold. A watch
    with a reason schedules the task until its deadline
    (``plans.scheduled_wait``); any other watch names the event ending it.
    """
    from plans import blocked_wait, holds, scheduled_wait, user_ask

    if blocked_wait.is_wait(operation, params):
        return watch_operations.arm_write(
            plan_id, blocked_wait.watch_params(params), actor, common["db"],
        )
    if scheduled_wait.is_scheduled(operation, params):
        return watch_operations.arm_write(
            plan_id, scheduled_wait.watch_params(params), actor, common["db"],
        )
    if operation == "pause_node":
        holds.pause_hold(
            params["node_id"], fields.text(params, "reason"),
            **fields.expected(params), **common,
        )
        return None
    if operation == "block_node":
        ask = None
        if user_ask.is_user_block(operation, params):
            plan = service._plan_row(common["db"], plan_id)
            ask = user_ask.require(common["db"], plan, params["node_id"], params, actor)
        holds.block_hold(
            params["node_id"],
            reason_key=fields.text(params, "reason_key"),
            reason=fields.text(params, "reason"),
            owner_kind=fields.text(params, "owner_kind"),
            # A user ask is the specific owner of this hold. Reusing owner_id
            # ties its clearance to this ask even if the task waits again later.
            owner_id=ask if ask is not None else params.get("owner_id"),
            # Where a user ask was raised, so chat shows the ask on that step.
            raised_in=_raised_in(actor) if ask is not None else None,
            **fields.expected(params), **common,
        )
        if ask is not None:
            user_ask.attach(
                common["db"], params["node_id"], ask,
                actor_id=common["actor_id"], provenance=common["provenance"],
            )
        return None
    scheduled_wait.require_event(params)
    return watch_operations.arm_write(plan_id, params, actor, common["db"])


def after_park(plan_id: str, operation: str, params: dict[str, Any], actor, common: dict) -> dict:
    """Park a task, start the named next task, reconcile, and report next.

    The same order as a close: the park releases the task's selection, the
    named start runs next, and reconciliation runs last, so the task the
    caller chose is offered the slot ahead of already-active queued work.
    """
    from plans import admission, close_next, delegation_harvest, user_ask

    db = common["db"]
    node_id = params["node_id"]
    parked = service._node_row(db, node_id)
    slot = close_next.slot_before(db, parked)
    owed = (
        operation in ("arm_watch", "block_node")
        and not user_ask.is_user_block(operation, params)
        and delegation_harvest.awaiting_owner_verdict(db, parked)
    )
    watch = _park_write(plan_id, operation, params, actor, common)
    named = None
    if "next_node_id" in params:
        named = close_next.start_named(
            db, fields.text(params, "next_node_id"), slot,
            actor_id=common["actor_id"], provenance=common["provenance"],
        )
    acting = actor.thread_id if actor.kind == "thread" else None
    if watch is not None:
        result = watch_operations.finish_arm(plan_id, params, actor, watch, db)
        from plans import blocked_wait, event_wait, scheduled_wait

        if operation == "block_node":
            result["wait"] = blocked_wait.explain(watch)
        elif scheduled_wait.is_scheduled(operation, params):
            result["wait"] = scheduled_wait.explain(watch)
        else:
            result["wait"] = event_wait.explain(watch, params.get("condition"))
    else:
        admission.reconcile_plan(
            slot.plan_id, actor_id=common["actor_id"],
            provenance=common["provenance"], causing_executor=acting, db=db,
        )
        result = admission.result(node_id, db=db)
    result.update(close_next.next_fields(db, parked, slot, named, acting=acting))
    if owed:
        from prompts import plan_tools

        result["verdict_owed"] = plan_tools.VERDICT_OWED.format(
            title=str(parked["title"]), step=str(parked["id"]),
        )
    return result


def _release_archived(
    plan_id: str, archived: list[dict], common: dict, *, executor: str | None,
) -> None:
    """Take the executor off every task this archive retired, then reselect.

    One reconciliation covers the whole list: it reads the plan's current slots,
    so calling it per node would only repeat the same read.
    """
    from plans import admission

    db = common["db"]
    freed = [
        entry["node_id"] for entry in archived
        if admission.release(entry["node_id"], "archived", db=db)
    ]
    if not freed:
        return
    admission.reconcile_plan(
        plan_id, actor_id=common["actor_id"], provenance=common["provenance"],
        causing_executor=executor, db=db,
    )


def archive_many(
    plan_id: str, node_ids: list[str], common: dict, db, *, executor: str | None,
) -> dict:
    """Retire many nodes, reporting every refusal in the caller's order.

    A lane-owned node is refused here, per node, rather than aborting the call
    at authorization the way a single archive is: the lane rule is the
    boundary's, so it is judged here, and ``plans.archival`` judges the rest.

    Archiving a selected task takes its executor off that task, in bulk exactly
    as singly: an attempt left open on an archived node holds the slot against
    every later selection, and nothing would reopen it.
    """
    import plans.archival as archival
    from plans import node_settlement

    lanes = {
        node_id: node_settlement.lane_refusing(db, node_id, "archive_node")
        for node_id in node_ids
    }
    report = archival.archive_nodes(
        [node_id for node_id in node_ids if lanes[node_id] is None], **common,
    )
    _release_archived(plan_id, report["archived"], common, executor=executor)
    reasons = {entry["node_id"]: entry["reason"] for entry in report["refused"]}
    for node_id, lane in lanes.items():
        if lane is not None:
            reasons[node_id] = node_settlement.refusal_message(lane, "archive_node")
    return {
        "archived": report["archived"],
        "refused": [
            {"node_id": node_id, "reason": reasons[node_id]}
            for node_id in dict.fromkeys(node_ids) if node_id in reasons
        ],
    }
