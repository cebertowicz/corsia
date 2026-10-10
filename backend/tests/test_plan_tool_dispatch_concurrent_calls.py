"""Concurrent calls under one selection, and a requirements edit while one runs.

A selection stays valid while its own calls are open: the validity predicate
never treats an attempt's own open dispatch as invalidating. Two calls
under one valid selection are both recorded under it, and settling the first
leaves the attempt selected. A requirements edit is different: it genuinely
invalidates the attempt, so it releases it at once and reselects the task at
the new revision even while an already-started call is open (#2705). The open
call stays attributed to the stale attempt, and a concurrent call is recorded
under the new one, with no refusal and no manual `start`.
"""

from __future__ import annotations

import pytest

from infra.database import get_db
from plans import admission, service
from plans.boundary import mutate
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import (
    BlockingProbe,
    ProbeTool,
    abandon_blocking_call,
    add_task,
    execute_as,
    finish_blocking_call,
    open_turn,
    registry_of,
    start_blocking_call,
    thread_actor,
)


def _attempt_row(db, attempt_id: str):
    return db.execute(
        "SELECT * FROM plan_node_attempts WHERE id=?", (attempt_id,),
    ).fetchone()


def _dispatched(db, turn_id: str) -> list[tuple]:
    """Return (tool name, attempt id, outcome) for every call recorded in one Turn."""
    rows = db.execute(
        """SELECT tool_name,attempt_id,outcome FROM plan_tool_dispatches
           WHERE turn_id=? ORDER BY started_at,id""",
        (turn_id,),
    ).fetchall()
    return [(row["tool_name"], row["attempt_id"], row["outcome"]) for row in rows]


def _selected_task(db, thread_id: str, title: str):
    """Open a Turn, then select one task; return identity, plan, node, attempt id."""
    identity = open_turn(db, thread_id)
    plan = service.ensure_plan("thread", thread_id, db=db)
    node = add_task(db, plan.id, title)
    activated = admission.activate(
        node.id, expected_revision=node.revision, actor_id=thread_id,
        provenance="test", db=db,
    )
    db.commit()
    return identity, plan, activated, activated["admission"]["selection"]["attempt_id"]


@pytest.mark.asyncio
async def test_two_concurrent_calls_are_recorded_and_settling_one_keeps_the_selection(
    isolated_db,
):
    """An attempt's own open dispatch never invalidates it."""
    thread_id = mk_session("concurrent-valid-calls")
    db = get_db()
    identity, _plan, activated, attempt_id = _selected_task(db, thread_id, "Shared work")
    first, second = BlockingProbe("first_probe"), BlockingProbe("second_probe")
    registry = registry_of(first, second)
    first_call = await start_blocking_call(registry, identity, thread_id, first)
    second_call = await start_blocking_call(registry, identity, thread_id, second)
    try:
        assert _dispatched(db, identity.turn_id) == [
            (first.name, attempt_id, None), (second.name, attempt_id, None),
        ]

        assert await finish_blocking_call(first, first_call) == "ok"

        projected = admission.project(activated["node"]["id"], db=db)
        assert _attempt_row(db, attempt_id)["released_at"] is None
        assert projected["condition"] == "running"
        assert projected["selection"]["attempt_id"] == attempt_id
        assert projected["selection"]["in_flight_dispatch_count"] == 1

        assert await finish_blocking_call(second, second_call) == "ok"
    finally:
        await abandon_blocking_call(first, first_call)
        await abandon_blocking_call(second, second_call)

    assert _dispatched(db, identity.turn_id) == [
        (first.name, attempt_id, "succeeded"), (second.name, attempt_id, "succeeded"),
    ]
    assert _attempt_row(db, attempt_id)["released_at"] is None
    db.close()


def _edit_requirements(db, plan_id: str, node, actor_thread: str):
    """Bump one task's requirements revision through the public boundary."""
    return mutate(
        plan_id,
        "edit",
        {
            "node_id": node["id"],
            "acceptance_criteria": "The revised requirement is verified",
            "expected_revision": node["revision"],
        },
        actor=thread_actor(actor_thread),
        provenance="test.concurrent_calls",
        mutation_id="edit-requirements",
        db=db,
    ).result


@pytest.mark.asyncio
async def test_requirements_edit_under_an_in_flight_call_reselects_at_once(
    isolated_db,
):
    """A requirements edit reselects at once; the open call keeps its stale attempt."""
    thread_id = mk_session("binding-in-flight")
    db = get_db()
    identity, plan, activated, stale_attempt = _selected_task(db, thread_id, "In-flight work")
    blocking, probe = BlockingProbe(), ProbeTool()
    registry = registry_of(blocking, probe)
    in_flight = await start_blocking_call(registry, identity, thread_id, blocking)
    try:
        edited = _edit_requirements(db, plan.id, activated["node"], thread_id)
        db.commit()

        # The open call does not hold the stale attempt: the edit reselects now.
        reselected = edited["admission"]["selection"]
        assert _attempt_row(db, stale_attempt)["release_reason"] == "requirements_changed"
        assert edited["admission"]["condition"] == "running"
        assert reselected["attempt_id"] != stale_attempt
        assert reselected["requirements_revision"] == 2

        concurrent = await execute_as(registry, identity, thread_id, probe.name)

        assert concurrent == "ok"
        assert probe.calls == 1

        assert await finish_blocking_call(blocking, in_flight) == "ok"
    finally:
        await abandon_blocking_call(blocking, in_flight)

    # Settling the stale call changes nothing about the new selection.
    settled = admission.project(activated["node"]["id"], db=db)
    assert settled["condition"] == "running"
    assert settled["selection"]["attempt_id"] == reselected["attempt_id"]

    assert _dispatched(db, identity.turn_id) == [
        (blocking.name, stale_attempt, "succeeded"),
        (probe.name, reselected["attempt_id"], "succeeded"),
    ]
    db.close()
