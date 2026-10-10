"""A closing operation can start the task the agent names next, in the same call.

`next_node_id` behaves exactly like the agent activating (pending) or starting
(active) that task itself, evaluated after the close so the close's own effects
(a freed slot, an accepted prerequisite) count, and before reconcile so the
agent's choice beats auto-selection of earlier queued work. The started task is
`next.selected` with `how: started` and its acceptance criteria. A replay
returns the recorded result and never starts anything twice.
"""

from __future__ import annotations

import pytest

from plans import service
from tests.plan_close_next_helpers import (
    HUMAN,
    assert_block_shape,
    assert_rows_exact,
    attempt_count,
    close,
    close_params,
    listed,
    next_block,
    open_attempt,
    owner_plan,
    plan_update,
    queue_active,
    receipt_count,
    request_review,
    row,
    row_ids,
    select,
    task,
)
from tests.test_plan_reconcile_release import db  # noqa: F401 - fixture


def _queued_scene(db):
    """Select Task A, queue earlier active work behind it, and leave Task B pending."""
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    earlier = task(db, plan.id, "Earlier queued work")
    task_b = task(db, plan.id, "Task B")
    select(db, plan.id, task_a.id)
    queue_active(db, earlier.id)
    return plan, task_a, earlier, task_b


def _assert_started(block, node_id: str, criteria: str) -> None:
    named = block["named"]
    assert (named["started"], named["refusal"], named["reason"]) == (True, None, None)
    selected = block["selected"]
    assert (selected["id"], selected["how"], selected["state"], selected["wait"]) == (
        node_id, "started", "active", None,
    )
    assert selected["acceptance_criteria"] == criteria


def test_a_named_pending_task_starts_in_the_close_and_beats_auto_selection(db):
    plan, task_a, earlier, task_b = _queued_scene(db)

    result = close(db, plan.id, "request_review", task_a.id, next_node_id=task_b.id)

    block = next_block(result)
    assert_block_shape(block, named=True)
    _assert_started(block, task_b.id, "Task B is verified")
    assert result["node"]["state"] == "review_requested"
    assert open_attempt(db, task_b.id)["executor_thread_id"] == "owner"
    assert open_attempt(db, earlier.id) is None
    assert row_ids(block) == [earlier.id]
    assert block["open_tasks_listed"] == listed(1, 1)
    assert_rows_exact(db, block)


def test_a_padded_next_node_id_names_the_same_task(db):
    plan, task_a, _earlier, task_b = _queued_scene(db)

    block = next_block(
        close(db, plan.id, "request_review", task_a.id, next_node_id=f"  {task_b.id}\n"),
    )

    _assert_started(block, task_b.id, "Task B is verified")


def test_a_named_active_task_is_selected_over_earlier_queued_work(db):
    plan, task_a, earlier, task_b = _queued_scene(db)
    queue_active(db, task_b.id)

    block = next_block(close(db, plan.id, "fail", task_a.id, next_node_id=task_b.id))

    _assert_started(block, task_b.id, "Task B is verified")
    assert open_attempt(db, task_b.id) is not None
    assert open_attempt(db, earlier.id) is None
    assert_rows_exact(db, block)


@pytest.mark.parametrize("operation", ["accept", "accept_with_follow_up", "archive_node"])
def test_every_closing_operation_can_start_the_named_task(db, operation):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    task_b = task(db, plan.id, "Task B")
    select(db, plan.id, task_a.id)
    if operation != "archive_node":
        request_review(db, plan.id, task_a.id)

    block = next_block(close(db, plan.id, operation, task_a.id, next_node_id=task_b.id))

    _assert_started(block, task_b.id, "Task B is verified")


def test_the_named_task_is_judged_after_the_close_so_its_accepted_prerequisite_counts(db):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    task_b = task(db, plan.id, "Task B")
    service.add_dependency(task_b.id, task_a.id, db=db)
    db.commit()
    select(db, plan.id, task_a.id)
    request_review(db, plan.id, task_a.id)

    block = next_block(close(db, plan.id, "accept", task_a.id, next_node_id=task_b.id))

    _assert_started(block, task_b.id, "Task B is verified")


def test_a_human_actor_may_name_the_next_task_under_the_same_authority_as_activate(db):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    task_b = task(db, plan.id, "Task B")
    select(db, plan.id, task_a.id)

    block = next_block(
        close(db, plan.id, "request_review", task_a.id, next_node_id=task_b.id, actor=HUMAN),
    )

    _assert_started(block, task_b.id, "Task B is verified")
    assert open_attempt(db, task_b.id)["executor_thread_id"] == "owner"


@pytest.mark.asyncio
async def test_the_http_route_returns_the_same_block_for_the_authenticated_human(
    db, monkeypatch,
):
    from api import plans as api_plans
    from tests.test_attention_queue_api import _DbProxy, _immediate

    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    task_b = task(db, plan.id, "Task B")
    select(db, plan.id, task_a.id)
    monkeypatch.setattr(api_plans, "get_db", lambda: _DbProxy(db))
    monkeypatch.setattr(api_plans, "run_in_db", _immediate)

    response = await api_plans.mutate_plan(plan.id, {
        "operation": "request_review", "mutation_id": "http-close-a",
        "params": close_params(db, "request_review", task_a.id, next_node_id=task_b.id),
    }, {"id": "user"})

    block = next_block(response)
    assert_block_shape(block, named=True)
    _assert_started(block, task_b.id, "Task B is verified")


def test_a_replayed_named_close_returns_the_recorded_result_and_starts_nothing_again(db):
    plan, task_a, _earlier, task_b = _queued_scene(db)
    params = close_params(db, "request_review", task_a.id, next_node_id=task_b.id)

    first = plan_update(db, plan.id, "request_review", params, mutation_id="close-a")
    receipts = receipt_count(db)
    replay = plan_update(db, plan.id, "request_review", params, mutation_id="close-a")

    assert replay == first
    assert first["next"]["named"]["started"] is True
    assert receipt_count(db) == receipts
    assert attempt_count(db, task_b.id) == 1


def test_a_replayed_refusal_is_never_retried_even_after_its_cause_is_gone(db):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    running = task(db, plan.id, "Running work")
    task_b = task(db, plan.id, "Task B")
    select(db, plan.id, task_a.id)
    queue_active(db, running.id)
    request_review(db, plan.id, task_a.id)
    params = close_params(db, "accept", task_a.id, next_node_id=task_b.id)
    first = plan_update(db, plan.id, "accept", params, mutation_id="accept-a")
    close(db, plan.id, "fail", running.id)

    replay = plan_update(db, plan.id, "accept", params, mutation_id="accept-a")

    assert first["next"]["named"]["refusal"] == "slot_held"
    assert replay == first
    assert row(db, task_b.id)["state"] == "pending"
    assert attempt_count(db, task_b.id) == 0


def test_reusing_a_close_mutation_id_with_a_different_next_task_is_a_different_request(db):
    plan, task_a, earlier, task_b = _queued_scene(db)
    params = close_params(db, "request_review", task_a.id, next_node_id=task_b.id)
    plan_update(db, plan.id, "request_review", params, mutation_id="close-a")

    with pytest.raises(service.PlanError, match="different request"):
        plan_update(
            db, plan.id, "request_review", dict(params, next_node_id=earlier.id),
            mutation_id="close-a",
        )
