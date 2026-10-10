"""Finish Task A and continue with Task B in the same Turn, without a plan read.

The real `plan_update` tool runs through the real registry under Task A's
selection. Closing A without naming a successor lists B with the exact revision
one `activate` needs; naming B starts it inside the one close call. Either way
the next call attaches to B while the Turn keeps naming A. Plans' own tools
record no dispatch, because their effect commits in the same write, so the
close never defers A's release; only another call still running on A holds the
slot, and then the named start reports it. An internal break in the named start
surfaces as an internal error, not a retryable plan conflict.
"""

from __future__ import annotations

import json

import pytest

from infra.database import get_db
from plans import admission, service, tool_dispatches
from tests.event_routing_helpers import mk_session
from tests.plan_close_next_helpers import (
    break_named_start,
    close_params,
    open_attempt,
    receipt_count,
    row,
    seed_dispatch,
)
from tests.plan_execution_helpers import (
    ProbeTool,
    add_task,
    dispatch_rows,
    execute_as,
    open_turn,
    registry_of,
    turn_accounting,
)
from tools.plans import PlanUpdateTool

CONFLICT_TEXT = "conflict, re-read the plan and retry"
INPUT_TEXT = "invalid request, fix and resend"


def _selected_in_turn(db, thread_id: str):
    """Open Turn T, then select Task A with Task B pending behind it."""
    identity = open_turn(db, thread_id)
    plan = service.ensure_plan("thread", thread_id, db=db)
    task_a = add_task(db, plan.id, "Task A", order_key=0)
    task_b = add_task(db, plan.id, "Task B", order_key=1)
    selected = admission.activate(
        task_a.id, expected_revision=task_a.revision, actor_id=thread_id,
        provenance="test", db=db,
    )
    db.commit()
    return identity, plan, task_a, task_b, selected["admission"]["selection"]["attempt_id"]


def _arguments(plan_id: str, operation: str, params: dict, mutation_id: str) -> dict:
    return {
        "plan_id": plan_id, "mutation_id": mutation_id,
        "operation": operation, "params": params,
    }


async def _call(registry, identity, thread_id, plan_id, operation, params, mutation_id):
    """Call the real plan_update tool through the registry; return the raw string."""
    return await execute_as(
        registry, identity, thread_id, "plan_update",
        _arguments(plan_id, operation, params, mutation_id),
    )


def _recorded(db, tool_name: str) -> list[tuple]:
    return [
        (item["attempt_id"], item["turn_id"], item["outcome"])
        for item in dispatch_rows(db) if item["tool_name"] == tool_name
    ]


@pytest.mark.asyncio
async def test_without_a_named_task_the_listed_revision_is_enough_to_activate_b(isolated_db):
    thread_id = mk_session("close-next-listed")
    db = get_db()
    identity, plan, task_a, task_b, attempt_a = _selected_in_turn(db, thread_id)
    accounting = turn_accounting(db, identity.turn_id)
    probe = ProbeTool()
    registry = registry_of(PlanUpdateTool(), probe)

    raw = await _call(
        registry, identity, thread_id, plan.id, "request_review",
        close_params(db, "request_review", task_a.id), "close-a",
    )
    listed_b = json.loads(raw)["next"]["open_tasks"][0]
    activated = json.loads(await _call(
        registry, identity, thread_id, plan.id, "activate",
        {"node_id": listed_b["id"], "expected_revision": listed_b["revision"]}, "activate-b",
    ))

    assert (listed_b["id"], listed_b["state"]) == (task_b.id, "pending")
    assert activated["admission"]["condition"] == "running"
    assert await execute_as(registry, identity, thread_id, probe.name) == "ok"
    attempt_b = activated["admission"]["selection"]["attempt_id"]
    assert _recorded(db, probe.name) == [(attempt_b, identity.turn_id, "succeeded")]
    assert turn_accounting(db, identity.turn_id) == accounting
    assert turn_accounting(db, identity.turn_id)[2] == attempt_a
    db.close()


@pytest.mark.asyncio
async def test_naming_b_starts_it_in_the_one_close_call_and_the_next_call_attaches_to_b(
    isolated_db,
):
    thread_id = mk_session("close-next-named")
    db = get_db()
    identity, plan, task_a, task_b, attempt_a = _selected_in_turn(db, thread_id)
    accounting = turn_accounting(db, identity.turn_id)
    probe = ProbeTool()
    registry = registry_of(PlanUpdateTool(), probe)

    raw = await _call(
        registry, identity, thread_id, plan.id, "request_review",
        close_params(db, "request_review", task_a.id, next_node_id=task_b.id), "close-a",
    )

    block = json.loads(raw)["next"]
    assert block["named"]["started"] is True
    assert (block["selected"]["id"], block["selected"]["how"]) == (task_b.id, "started")
    assert admission.project(task_b.id, db=db)["condition"] == "running"
    assert _recorded(db, "plan_update") == []
    assert await execute_as(registry, identity, thread_id, probe.name) == "ok"
    attempt_b = open_attempt(db, task_b.id)["id"]
    assert _recorded(db, probe.name) == [(attempt_b, identity.turn_id, "succeeded")]
    assert turn_accounting(db, identity.turn_id) == accounting
    db.close()


@pytest.mark.asyncio
async def test_a_concurrent_call_on_a_never_stops_the_named_start(isolated_db):
    """An in-flight call on A does not hold A's slot, so B starts (#2705).

    The close releases A and the named start of B succeeds in the same call;
    the call on A later settles under A without touching B's selection.
    """
    thread_id = mk_session("close-next-deferred")
    db = get_db()
    identity, plan, task_a, task_b, attempt_a = _selected_in_turn(db, thread_id)
    in_flight = seed_dispatch(db, attempt_a)
    registry = registry_of(PlanUpdateTool())

    raw = await _call(
        registry, identity, thread_id, plan.id, "request_review",
        close_params(db, "request_review", task_a.id, next_node_id=task_b.id), "close-a",
    )

    named = json.loads(raw)["next"]["named"]
    assert (named["started"], named["refusal"]) == (True, None)
    assert row(db, task_a.id)["state"] == "review_requested"
    assert open_attempt(db, task_a.id) is None
    attempt_b = open_attempt(db, task_b.id)["id"]

    assert tool_dispatches.settle(in_flight, "succeeded") is True

    released = db.execute(
        "SELECT release_reason FROM plan_node_attempts WHERE id=?", (attempt_a,),
    ).fetchone()
    assert released["release_reason"] == "review_requested"
    assert open_attempt(db, task_b.id)["id"] == attempt_b
    db.close()


@pytest.mark.asyncio
async def test_an_internal_named_start_break_is_not_reported_as_a_retryable_conflict(
    isolated_db, monkeypatch,
):
    thread_id = mk_session("close-next-internal")
    db = get_db()
    identity, plan, task_a, task_b, attempt_a = _selected_in_turn(db, thread_id)
    registry = registry_of(PlanUpdateTool())
    receipts = receipt_count(db)
    break_named_start(monkeypatch)

    raw = await _call(
        registry, identity, thread_id, plan.id, "request_review",
        close_params(db, "request_review", task_a.id, next_node_id=task_b.id), "close-a",
    )

    assert raw.startswith("Error")
    assert CONFLICT_TEXT not in raw
    assert INPUT_TEXT not in raw
    # The call passed the tool's own schema and failed inside it; Plans' own
    # tools record no dispatch.
    assert _recorded(db, "plan_update") == []
    assert row(db, task_a.id)["state"] == "active"
    assert open_attempt(db, task_a.id)["id"] == attempt_a
    assert row(db, task_b.id)["state"] == "pending"
    assert receipt_count(db) == receipts
    db.close()
