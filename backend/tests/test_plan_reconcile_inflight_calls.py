"""With a real call in flight, a park still hands the slot on at once (#2705).

Task A is selected and one non-plan registry call is still running on A's
attempt when a writer makes A ineligible. The writer releases A and reconciles
in the same write: the next eligible task is selected with no `activate` or
`start`, and the executor's next call is recorded under it while A's call is
still running. When that call returns it settles under A's attempt, so its
effect stays attributed to A. A whole-plan pause selects nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from infra.database import get_db
from plans import admission, service
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
)
from tests.test_plan_reconcile_release import (
    HUMAN,
    attempt,
    open_attempt,
    plan_update,
    queued_pair,
    revision,
    seed_threads,
)


def _recorded(connection, tool_name: str) -> list[tuple]:
    """Return (attempt id, outcome) for one tool's dispatch rows."""
    return [
        (row["attempt_id"], row["outcome"])
        for row in dispatch_rows(connection) if row["tool_name"] == tool_name
    ]


def _ineligibility_params(connection, plan_id: str, operation: str, node_id: str) -> dict:
    """Build boundary params for a writer that makes the selected task ineligible."""
    params: dict[str, Any] = {"node_id": node_id}
    if operation == "add_dependency":
        params["depends_on_node_id"] = add_task(
            connection, plan_id, "Unaccepted prerequisite",
        ).id
    if operation == "arm_watch":
        params["condition"] = {"type": "deadline", "value": "2099-01-01T00:00:00+00:00"}
        params["expected_event"] = "proof.ready"
    params["expected_revision"] = revision(connection, node_id)
    return params


@pytest.mark.parametrize("operation", ["archive_node", "add_dependency", "arm_watch"])
async def test_ineligibility_with_a_call_in_flight_hands_the_slot_on_at_once(
    isolated_db, operation,
):
    """Archive, a new dependency and an armed watch each hand the slot on at once."""
    connection = get_db()
    blocking, probe = BlockingProbe(), ProbeTool()
    registry = registry_of(blocking, probe)
    try:
        seed_threads(connection)
        identity = open_turn(connection, "owner")
        plan, task_a, a_attempt, task_b = queued_pair(connection)
        connection.commit()
        call = await start_blocking_call(registry, identity, "owner", blocking)
        try:
            plan_update(
                connection, plan.id, operation,
                _ineligibility_params(connection, plan.id, operation, task_a),
            )

            assert attempt(connection, a_attempt)["released_at"] is not None
            successor = open_attempt(connection, task_b)
            assert successor is not None
            assert successor["executor_thread_id"] == "owner"
            # The executor's next call is recorded under B while A's still runs.
            assert await execute_as(registry, identity, "owner", probe.name) == "ok"
            assert _recorded(connection, probe.name) == [(successor["id"], "succeeded")]

            assert await finish_blocking_call(blocking, call) == "ok"
        finally:
            await abandon_blocking_call(blocking, call)

        assert _recorded(connection, blocking.name) == [(a_attempt, "succeeded")]
        assert open_attempt(connection, task_b)["id"] == successor["id"]
    finally:
        connection.close()


async def test_whole_plan_pause_with_a_call_in_flight_releases_and_selects_nothing(
    isolated_db,
):
    """A whole-plan pause releases A at once and selects nothing.

    The running call and a new call both run; the new one has no selection to
    attach to, and the running one settles under A's attempt. B stays
    unselected in the paused plan although the slot is empty.
    """
    connection = get_db()
    blocking, probe = BlockingProbe(), ProbeTool()
    registry = registry_of(blocking, probe)
    try:
        seed_threads(connection)
        identity = open_turn(connection, "owner")
        plan, task_a, a_attempt, task_b = queued_pair(connection)
        connection.commit()
        call = await start_blocking_call(registry, identity, "owner", blocking)
        try:
            paused = plan_update(
                connection, plan.id, "pause",
                {"expected_revision": service._plan_row(connection, plan.id)["revision"]},
                actor=HUMAN,
            )

            assert paused["plan"]["state"] == "paused"
            assert attempt(connection, a_attempt)["released_at"] is not None
            assert open_attempt(connection, task_b) is None
            assert await execute_as(registry, identity, "owner", probe.name) == "ok"
            assert _recorded(connection, probe.name) == []

            assert await finish_blocking_call(blocking, call) == "ok"
        finally:
            await abandon_blocking_call(blocking, call)

        assert _recorded(connection, blocking.name) == [(a_attempt, "succeeded")]
        assert open_attempt(connection, task_b) is None
        assert connection.execute(
            "SELECT COUNT(*) FROM plan_node_attempts WHERE plan_id=? AND released_at IS NULL",
            (plan.id,),
        ).fetchone()[0] == 0
        assert admission.project(task_b, db=connection)["condition"] == "paused"
    finally:
        connection.close()
