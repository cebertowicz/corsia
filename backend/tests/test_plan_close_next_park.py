"""Parking a task says what is next and can start the named task, exactly like a close.

Scheduling a task (`arm_watch` with a reason), `block_node` and `arm_watch`
take the executor off a task without closing it; an agent no longer pauses. Each returns the same `next` block a closing operation returns and
accepts the same optional `next_node_id`: without it the result names what now
holds the slot and lists the executor's other open tasks; with it the named task
starts in the same call, ahead of already-active queued work. The parked task
keeps its state and its cause, is left out of "other open tasks", and retakes
the slot on its own once it is picked up again and the slot is free.
"""

from __future__ import annotations

import pytest

from tests.plan_close_next_helpers import (
    assert_block_shape,
    assert_rows_exact,
    close,
    listed,
    next_block,
    open_attempt,
    owner_plan,
    plan_update,
    queue_active,
    revision,
    row,
    row_ids,
    select,
    task,
)
from tests.test_plan_reconcile_release import db  # noqa: F401 - fixture

#: Ways an agent takes the executor off a task without closing it; "schedule"
#: is `arm_watch` with a reason (``plans.scheduled_wait``).
PARK_OPERATIONS = ("schedule", "block_node", "arm_watch")


def _park_params(db, operation: str, node_id: str, **extra) -> dict:
    """Build a valid body for one park operation at the node's current revision."""
    params = {"node_id": node_id, "expected_revision": revision(db, node_id), **extra}
    if operation == "schedule":
        params.update(
            condition={"type": "deadline", "value": "2099-01-01T00:00:00+00:00"},
            reason="Switching to urgent work",
        )
    elif operation == "block_node":
        params.update(
            reason_key="missing_instructions", reason="A venue choice is required",
            owner_kind="user", owner_id="user",
        )
    else:
        params.update(
            condition={"type": "deadline", "value": "2099-01-01T00:00:00+00:00"},
            expected_event="proof.ready",
        )
    return params


def _park(db, plan_id: str, operation: str, node_id: str, **extra) -> dict:
    """Apply one park operation through the real boundary and return its result."""
    params = _park_params(db, operation, node_id, **extra)
    return plan_update(db, plan_id, "arm_watch" if operation == "schedule" else operation, params)


@pytest.mark.parametrize("operation", PARK_OPERATIONS)
def test_each_park_operation_returns_the_next_block_without_the_parked_task(db, operation):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    task_b = task(db, plan.id, "Task B")
    select(db, plan.id, task_a.id)

    block = next_block(_park(db, plan.id, operation, task_a.id))

    assert_block_shape(block, named=False)
    assert block["selected"] is None
    assert row_ids(block) == [task_b.id]
    assert block["open_tasks_listed"] == listed(1, 1)
    assert_rows_exact(db, block)
    assert row(db, task_a.id)["state"] == "active"
    assert open_attempt(db, task_a.id) is None


@pytest.mark.parametrize("operation", PARK_OPERATIONS)
def test_a_named_task_starts_in_the_park_call_ahead_of_queued_work(db, operation):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    earlier = task(db, plan.id, "Earlier queued work")
    task_b = task(db, plan.id, "Task B")
    select(db, plan.id, task_a.id)
    queue_active(db, earlier.id)

    block = next_block(_park(db, plan.id, operation, task_a.id, next_node_id=task_b.id))

    assert_block_shape(block, named=True)
    assert block["named"]["started"] is True
    assert (block["selected"]["id"], block["selected"]["how"]) == (task_b.id, "started")
    assert block["selected"]["acceptance_criteria"] == "Task B is verified"
    assert open_attempt(db, task_b.id)["executor_thread_id"] == "owner"
    assert open_attempt(db, earlier.id) is None
    assert row_ids(block) == [earlier.id]
    assert row(db, task_a.id)["state"] == "active"


def test_park_a_for_b_then_pick_a_up_and_it_retakes_the_slot_when_b_closes(db):
    """Schedule, hop, come back early: nothing is closed and nothing is started unasked."""
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    task_b = task(db, plan.id, "Task B")
    select(db, plan.id, task_a.id)

    parked = _park(db, plan.id, "schedule", task_a.id, next_node_id=task_b.id)
    watch_id = parked["admission"]["causes"][0]["watch_id"]
    resumed = plan_update(
        db, plan.id, "resolve_watch",
        {"node_id": task_a.id, "watch_id": watch_id, "expected_revision": revision(db, task_a.id)},
    )
    queued_behind_b = open_attempt(db, task_a.id)
    block = next_block(close(db, plan.id, "request_review", task_b.id))

    assert next_block(parked)["selected"]["id"] == task_b.id
    assert "next" not in resumed
    assert queued_behind_b is None
    assert (block["selected"]["id"], block["selected"]["how"]) == (task_a.id, "auto_selected")
    assert open_attempt(db, task_a.id)["executor_thread_id"] == "owner"
