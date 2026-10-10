"""Settle Task A and continue with Task B in the same Turn.

The real `plan_update` tool runs through the real registry and records no
dispatch: it is one of Plans' own tools, whose plan effect commits inside the
same write, so nothing of its call can hold A open. The transition releases A
with its own reason and reconciles, selecting queued Task B before
`plan_update` returns - no `activate`, `start`, or next Turn. The Turn keeps
naming A as write-once accounting while the next call is recorded under B.

A different call that can still carry an external effect (a concurrent
non-plan call on A) does defer A's release: A keeps its slot until that
call settles, and its per-call settlement releases A as 'dispatch_settled' and
selects B in the same write.
"""

from __future__ import annotations

import json

import pytest

from infra.database import get_db
from plans import admission, service
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import (
    BlockingProbe,
    ProbeTool,
    abandon_blocking_call,
    add_task,
    dispatch_rows,
    execute_as,
    finish_blocking_call,
    open_turn,
    registry_of,
    start_blocking_call,
    turn_accounting,
)
from tools.plans import PlanUpdateTool


def _attempt(db, attempt_id: str):
    return db.execute(
        "SELECT * FROM plan_node_attempts WHERE id=?", (attempt_id,),
    ).fetchone()


def _open_attempt_for(db, node_id: str):
    return db.execute(
        "SELECT * FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL",
        (node_id,),
    ).fetchone()


async def _plan_update(registry, identity, thread_id, plan_id, operation, params):
    """Call the real plan_update tool through the registry and decode it."""
    result = await execute_as(
        registry, identity, thread_id, "plan_update",
        {
            "plan_id": plan_id, "mutation_id": f"{operation}-{params['node_id']}",
            "operation": operation, "params": params,
        },
    )
    assert not result.startswith("Error"), result
    return json.loads(result)


def _selected_a_with_turn(db, thread_id: str):
    """Open Turn T, then select Task A so T's accounting names A."""
    identity = open_turn(db, thread_id)
    plan = service.ensure_plan("thread", thread_id, db=db)
    task_a = add_task(db, plan.id, "Task A", order_key=0)
    task_b = add_task(db, plan.id, "Task B", order_key=1)
    selected = admission.activate(
        task_a.id, expected_revision=task_a.revision, actor_id=thread_id,
        provenance="test", db=db,
    )
    db.commit()
    attempt_a = selected["admission"]["selection"]["attempt_id"]
    assert turn_accounting(db, identity.turn_id)[2] == attempt_a
    return identity, plan, task_a, task_b, attempt_a


def _queue_b(db, task_b) -> None:
    """Make B active and queued behind A's occupied executor slot."""
    service.transition_node(task_b.id, "active", db=db)
    db.commit()
    assert _open_attempt_for(db, task_b.id) is None


def _request_review_params(db, node_id: str) -> dict:
    return {
        "node_id": node_id,
        "evidence": "Task A focused proof passed",
        "expected_revision": service._node_row(db, node_id)["revision"],
    }


async def _settle_a(registry, identity, thread_id, db, plan_id, node_id, settlement):
    """Request review of A, and for ``accept`` accept it, through plan_update."""
    await _plan_update(
        registry, identity, thread_id, plan_id, "request_review",
        _request_review_params(db, node_id),
    )
    if settlement == "accept":
        await _plan_update(
            registry, identity, thread_id, plan_id, "accept",
            {
                "node_id": node_id, "verification_result": "passed",
                "expected_revision": service._node_row(db, node_id)["revision"],
            },
        )


def _recorded(db, tool_name: str) -> list[tuple]:
    """Return (attempt id, Turn id, outcome) for one tool's dispatch rows."""
    return [
        (row["attempt_id"], row["turn_id"], row["outcome"])
        for row in dispatch_rows(db) if row["tool_name"] == tool_name
    ]


@pytest.mark.asyncio
async def test_settling_a_inside_plan_update_selects_queued_b_in_the_same_turn(
    isolated_db,
):
    """Settling A inside `plan_update` selects queued B in the same Turn.

    `plan_update` leaves no dispatch that could defer A's release, so A is
    released by the transition as 'review_requested' and B is selected before
    the call returns.
    """
    thread_id = mk_session("handoff-queued-b")
    db = get_db()
    identity, plan, task_a, task_b, attempt_a = _selected_a_with_turn(db, thread_id)
    _queue_b(db, task_b)
    accounting = turn_accounting(db, identity.turn_id)
    probe = ProbeTool()
    registry = registry_of(PlanUpdateTool(), probe)

    await _plan_update(
        registry, identity, thread_id, plan.id, "request_review",
        _request_review_params(db, task_a.id),
    )

    # Plans' own tool records no dispatch: the transition itself released A.
    released_a = _attempt(db, attempt_a)
    assert released_a["release_reason"] == "review_requested"
    assert released_a["released_at"] is not None
    attempt_b = _open_attempt_for(db, task_b.id)
    assert attempt_b is not None
    assert admission.project(task_b.id, db=db)["condition"] == "running"
    assert turn_accounting(db, identity.turn_id) == accounting
    assert _recorded(db, "plan_update") == []

    assert await execute_as(registry, identity, thread_id, probe.name) == "ok"

    assert _recorded(db, probe.name) == [(attempt_b["id"], identity.turn_id, "succeeded")]
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("settlement", ["request_review", "accept"])
async def test_a_concurrent_non_plan_call_on_a_never_defers_the_handoff(
    isolated_db, settlement,
):
    """A concurrent non-plan call on A does not hold A's slot (#2705).

    A blocking non-plan call is open on A when `plan_update` settles A. A is
    released and B selected in that same `plan_update`, which runs without a
    dispatch of its own. The blocking call stays attributed to A and settles
    under A's attempt.
    """
    thread_id = mk_session(f"handoff-concurrent-{settlement}")
    db = get_db()
    identity, plan, task_a, task_b, attempt_a = _selected_a_with_turn(db, thread_id)
    _queue_b(db, task_b)
    accounting = turn_accounting(db, identity.turn_id)
    blocking, probe = BlockingProbe(), ProbeTool()
    registry = registry_of(PlanUpdateTool(), blocking, probe)
    call = await start_blocking_call(registry, identity, thread_id, blocking)
    try:
        await _settle_a(registry, identity, thread_id, db, plan.id, task_a.id, settlement)

        # The concurrent call keeps its row under A but not A's slot.
        assert _attempt(db, attempt_a)["released_at"] is not None
        assert _open_attempt_for(db, task_b.id) is not None
        assert _recorded(db, blocking.name) == [(attempt_a, identity.turn_id, None)]
        assert _recorded(db, "plan_update") == []

        assert await finish_blocking_call(blocking, call) == "ok"
    finally:
        await abandon_blocking_call(blocking, call)

    assert _recorded(db, blocking.name) == [(attempt_a, identity.turn_id, "succeeded")]
    assert _attempt(db, attempt_a)["release_reason"] == "review_requested"
    attempt_b = _open_attempt_for(db, task_b.id)
    assert attempt_b is not None
    assert admission.project(task_b.id, db=db)["condition"] == "running"
    assert turn_accounting(db, identity.turn_id) == accounting

    assert await execute_as(registry, identity, thread_id, probe.name) == "ok"

    assert _recorded(db, probe.name) == [(attempt_b["id"], identity.turn_id, "succeeded")]
    db.close()


@pytest.mark.asyncio
async def test_activating_pending_b_after_settling_a_runs_in_the_same_turn(
    isolated_db,
):
    """Activating pending B is never refused by Turn accounting."""
    thread_id = mk_session("handoff-pending-b")
    db = get_db()
    identity, plan, task_a, task_b, attempt_a = _selected_a_with_turn(db, thread_id)
    probe = ProbeTool()
    registry = registry_of(PlanUpdateTool(), probe)

    await _plan_update(
        registry, identity, thread_id, plan.id, "request_review",
        _request_review_params(db, task_a.id),
    )
    assert _attempt(db, attempt_a)["released_at"] is not None
    activated = await _plan_update(
        registry, identity, thread_id, plan.id, "activate",
        {"node_id": task_b.id, "expected_revision": task_b.revision},
    )

    assert activated["node"]["state"] == "active"
    assert activated["admission"]["condition"] == "running"
    assert activated["admission"]["conflict"] is None
    attempt_b = activated["admission"]["selection"]["attempt_id"]
    assert admission.project(task_b.id, db=db)["conflict"] is None

    assert await execute_as(registry, identity, thread_id, probe.name) == "ok"

    assert _recorded(db, probe.name) == [(attempt_b, identity.turn_id, "succeeded")]
    assert turn_accounting(db, identity.turn_id)[2] == attempt_a
    db.close()
