"""Every closing result names what is next for the closed task's executor.

After request_review, mark_done, fail, accept, accept_with_follow_up or
archive_node the result carries `next`: the task now holding that executor's
slot in this plan and how it got there, up to three other open tasks with exact
id and revision in canonical depth-first plan order, and a self-describing
"k of N other open tasks" label. Nothing pending is auto-activated; an already
active task that reconcile selects is named as `auto_selected`.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from infra.schema import SCHEMA_SQL
from plans import service
from tests.plan_close_next_helpers import (
    CLOSING_OPERATIONS,
    assert_block_shape,
    assert_rows_exact,
    close,
    close_params,
    idle_listed,
    listed,
    next_block,
    open_attempt,
    owner_plan,
    pause,
    plan_update,
    queue_active,
    request_review,
    revision,
    row_ids,
    seed_dispatch,
    select,
    serialized,
    task,
)
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401 - fixture
from tests.plan_writer_authorizer import raw_plan_seed


def _closable(db, operation: str):
    """Return (plan, task A) with A in the state the operation closes from."""
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    task(db, plan.id, "Task B")
    select(db, plan.id, task_a.id)
    if operation in {"accept", "accept_with_follow_up"}:
        request_review(db, plan.id, task_a.id)
    return plan, task_a


@pytest.mark.parametrize("operation", CLOSING_OPERATIONS)
def test_each_closing_operation_returns_the_next_block(db, operation):
    plan, task_a = _closable(db, operation)

    block = next_block(close(db, plan.id, operation, task_a.id))

    assert_block_shape(block, named=False)
    assert block["executor_thread_id"] == "owner"
    assert_rows_exact(db, block)


def _non_closing(db, plan, task_a, task_b, operation) -> dict:
    """Apply one non-closing operation to the scene."""
    if operation == "reject":
        request_review(db, plan.id, task_a.id)
        params = {"node_id": task_a.id, "feedback": "Needs another pass"}
    elif operation == "start":
        queue_active(db, task_b.id)
        params = {"node_id": task_b.id}
    else:
        params = {"node_id": task_a.id, "title": "Task A renamed"}
    params["expected_revision"] = revision(db, params["node_id"])
    return plan_update(db, plan.id, operation, params)


@pytest.mark.parametrize("operation", ["reject", "start", "edit"])
def test_non_closing_operations_return_no_next_block(db, operation):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    task_b = task(db, plan.id, "Task B")
    select(db, plan.id, task_a.id)

    result = _non_closing(db, plan, task_a, task_b, operation)

    assert "next" not in result


def _settled_other_work(db, plan) -> None:
    """Add accepted, review-requested, archived and phase nodes before Task A."""
    accepted = task(db, plan.id, "Accepted work")
    select(db, plan.id, accepted.id)
    request_review(db, plan.id, accepted.id)
    plan_update(db, plan.id, "accept", close_params(db, "accept", accepted.id))
    in_review = task(db, plan.id, "Work in review")
    select(db, plan.id, in_review.id)
    request_review(db, plan.id, in_review.id)
    archived = task(db, plan.id, "Archived work")
    plan_update(
        db, plan.id, "archive_node",
        {"node_id": archived.id, "expected_revision": revision(db, archived.id)},
    )
    service.add_node(
        plan.id, kind="phase", title="Empty phase",
        acceptance_criteria="Phase is verified", db=db,
    )
    db.commit()


def test_open_tasks_are_the_owners_open_pending_and_active_leaves(db):
    plan = owner_plan(db)
    _settled_other_work(db, plan)
    task_a = task(db, plan.id, "Task A")
    task_b = task(db, plan.id, "Task B")
    task_c = task(db, plan.id, "Task C")
    task_d = task(db, plan.id, "Task D")
    service.add_dependency(task_d.id, task_b.id, db=db)
    task(db, plan.id, "User task", assignee="user")
    db.commit()
    select(db, plan.id, task_a.id)

    block = next_block(close(db, plan.id, "request_review", task_a.id))

    assert_block_shape(block, named=False)
    assert block["selected"] is None
    assert row_ids(block) == [task_b.id, task_c.id, task_d.id]
    assert block["open_tasks_listed"] == listed(3, 3)
    waits = [item["wait"] for item in block["open_tasks"]]
    assert waits[:2] == [None, None]
    assert waits[2].startswith("blocked: Waiting for")
    assert_rows_exact(db, block)


def _set_order(db, keys: dict[str, int]) -> None:
    for node_id, order_key in keys.items():
        db.execute("UPDATE plan_nodes SET order_key=? WHERE id=?", (order_key, node_id))
    db.commit()


def test_open_tasks_follow_depth_first_order_with_an_id_tie_break(db):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    phase = service.add_node(
        plan.id, kind="phase", title="Phase P", acceptance_criteria="Phase P is verified", db=db,
    )
    second = task(db, plan.id, "Second in phase", parent_id=phase.id)
    first = task(db, plan.id, "First in phase", parent_id=phase.id)
    tie_x, tie_y = task(db, plan.id, "Tie X"), task(db, plan.id, "Tie Y")
    last = task(db, plan.id, "Last root task")
    retired = service.add_node(
        plan.id, kind="phase", title="Retired phase", acceptance_criteria="Retired is verified", db=db,
    )
    stranded = task(db, plan.id, "Unreachable task", parent_id=retired.id)
    _set_order(db, {
        tie_x.id: 5, tie_y.id: 5, phase.id: 6, first.id: 0, second.id: 1,
        last.id: 7, retired.id: 8, task_a.id: 9,
    })
    # A legacy row: its parent is archived, so it follows the tree in canonical
    # order and is still this executor's open work.
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET archived_at=? WHERE id=?", (service._now(), retired.id))
    db.commit()
    select(db, plan.id, task_a.id)
    low, high = sorted([tie_x.id, tie_y.id])

    after_a = next_block(close(db, plan.id, "request_review", task_a.id))
    after_low = next_block(close(db, plan.id, "fail", low))
    after_high = next_block(close(db, plan.id, "fail", high))
    after_first = next_block(close(db, plan.id, "fail", first.id))

    assert row_ids(after_a) == [low, high, first.id]
    assert after_a["open_tasks_listed"] == listed(3, 6)
    assert row_ids(after_low) == [high, first.id, second.id]
    assert after_low["open_tasks_listed"] == listed(3, 5)
    assert row_ids(after_high) == [first.id, second.id, last.id]
    assert after_high["open_tasks_listed"] == listed(3, 4)
    assert row_ids(after_first)[-1] == stranded.id


def test_the_count_excludes_the_selected_and_the_closed_task(db):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    queued = task(db, plan.id, "Queued work")
    pending = [task(db, plan.id, f"Pending {index}") for index in range(6)]
    select(db, plan.id, task_a.id)
    queue_active(db, queued.id)

    block = next_block(close(db, plan.id, "request_review", task_a.id))

    assert block["selected"]["id"] == queued.id
    assert row_ids(block) == [node.id for node in pending[:3]]
    assert block["open_tasks_listed"] == listed(3, 6)


def test_an_already_active_task_reconcile_selects_is_named_auto_selected_with_its_criteria(db):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    queued = task(db, plan.id, "Queued work", description="Queued description body stays out")
    select(db, plan.id, task_a.id)
    queue_active(db, queued.id)

    block = next_block(close(db, plan.id, "request_review", task_a.id))

    selected = block["selected"]
    assert_block_shape(block, named=False)
    assert (selected["id"], selected["how"], selected["wait"]) == (queued.id, "auto_selected", None)
    assert selected["acceptance_criteria"] == "Queued work is verified"
    assert "Queued description body" not in serialized(block)
    assert open_attempt(db, queued.id) is not None
    assert block["open_tasks"] == []
    assert block["open_tasks_listed"] == listed(0, 0)
    assert_rows_exact(db, block)


def test_a_selection_that_was_already_running_before_the_close_is_named_running(db):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    queued = task(db, plan.id, "Queued work")
    select(db, plan.id, task_a.id)
    queue_active(db, queued.id)
    request_review(db, plan.id, task_a.id)
    running = open_attempt(db, queued.id)["id"]

    block = next_block(close(db, plan.id, "accept", task_a.id))

    assert (block["selected"]["id"], block["selected"]["how"]) == (queued.id, "running")
    assert block["selected"].get("acceptance_criteria") is None
    assert open_attempt(db, queued.id)["id"] == running


def test_an_in_flight_call_on_the_closed_task_does_not_hold_the_slot(db):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    queued = task(db, plan.id, "Queued work")
    attempt_a = select(db, plan.id, task_a.id)
    queue_active(db, queued.id)
    seed_dispatch(db, attempt_a)

    block = next_block(close(db, plan.id, "request_review", task_a.id))

    selected = block["selected"]
    assert (selected["id"], selected["how"]) == (queued.id, "auto_selected")
    assert selected["wait"] is None
    assert open_attempt(db, task_a.id) is None
    assert open_attempt(db, queued.id) is not None
    assert attempt_a != open_attempt(db, queued.id)["id"]


def test_held_and_watched_tasks_are_listed_with_their_wait_and_stay_held(db):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    paused = task(db, plan.id, "Paused work")
    watched = task(db, plan.id, "Watched work")
    select(db, plan.id, task_a.id)
    queue_active(db, paused.id)
    pause(db, plan.id, paused.id)
    queue_active(db, watched.id)
    # Inside the week a deadline may reach (W4), so it is kept as given.
    deadline = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0).isoformat()
    plan_update(db, plan.id, "arm_watch", {
        "node_id": watched.id, "expected_revision": revision(db, watched.id),
        "condition": {"type": "deadline", "value": deadline},
        "expected_event": "proof.ready",
    })

    block = next_block(close(db, plan.id, "request_review", task_a.id))

    assert block["selected"] is None
    # Nothing is ready, so this is the idle listing (W9): the timed wait
    # comes before the paused task, and neither is left out.
    assert row_ids(block) == [watched.id, paused.id]
    assert block["open_tasks_listed"] == idle_listed(2, 2)
    assert block["open_tasks"][0]["wait"].startswith("waiting: ")
    assert block["open_tasks"][1]["wait"].startswith("paused: ")
    assert block["open_tasks"][0]["ends"].startswith("ends in ")
    assert block["open_tasks"][1]["ends"] is None
    assert block["idle"] == "Nothing is active. If a waiting task can move now, start it."
    # A deadline is a moment to wait until, carried as the exact instant the
    # pane renders in the viewer's own time.
    assert f"Waiting until {deadline}" in block["open_tasks"][0]["wait"]
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL",
        (paused.id,),
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT status FROM expected_event_watches WHERE plan_node_id=?", (watched.id,),
    ).fetchone()["status"] == "armed"


def test_an_accepted_follow_up_is_listed_and_counted(db):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    task_b = task(db, plan.id, "Task B")
    select(db, plan.id, task_a.id)
    request_review(db, plan.id, task_a.id)

    result = close(db, plan.id, "accept_with_follow_up", task_a.id)

    block = next_block(result)
    assert row_ids(block) == [task_b.id, result["follow_up"]["id"]]
    assert block["open_tasks_listed"] == listed(2, 2)


def test_a_plan_with_nothing_else_open_says_zero_of_zero(db):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    select(db, plan.id, task_a.id)

    block = next_block(close(db, plan.id, "request_review", task_a.id))

    assert (block["selected"], block["open_tasks"]) == (None, [])
    assert block["open_tasks_listed"] == listed(0, 0)


def _fresh_db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    seed_threads(connection)
    return connection


def _wake_and_hold_facts(connection) -> dict[str, list]:
    """Normalize the rows a close must not add or clear, without generated ids."""
    queries = {
        "triggers": "SELECT kind,thread_id,state,reason FROM continuation_triggers",
        "wakes": "SELECT thread_id,status,cycle,last_open_count FROM plan_liveness_wakes",
        "holds": "SELECT kind,reason,cleared_at IS NULL FROM plan_node_holds",
    }
    return {
        name: sorted(tuple(item) for item in connection.execute(sql).fetchall())
        for name, sql in queries.items()
    }


def _held_scene_close(connection, next_node: str | None) -> dict:
    """Close Task A beside a paused queued task, naming `next_node` when given."""
    plan = owner_plan(connection)
    task_a = task(connection, plan.id, "Task A")
    paused = task(connection, plan.id, "Paused work")
    select(connection, plan.id, task_a.id)
    queue_active(connection, paused.id)
    pause(connection, plan.id, paused.id)
    target = {"paused": paused.id, "unknown": "no-such-node"}.get(next_node or "")
    if target is None:
        close(connection, plan.id, "request_review", task_a.id)
    else:
        close(connection, plan.id, "request_review", task_a.id, next_node_id=target)
    return _wake_and_hold_facts(connection)


@pytest.mark.parametrize("next_node", ["paused", "unknown"])
def test_a_refused_named_start_wakes_nothing_and_clears_no_hold(next_node):
    plain, named = _fresh_db(), _fresh_db()
    try:
        assert _held_scene_close(named, next_node) == _held_scene_close(plain, None)
    finally:
        plain.close()
        named.close()
