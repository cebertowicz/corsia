"""An idle executor's `next` block lists its waits, soonest-ending first (W9).

When a plan call leaves the executor with nothing in its slot and nothing
ready, the block it already returns lists every open task, waits with an end
first and soonest first, each with when it ends. It tells the agent to start a
waiting task that can move now, unless a wait ends within 15 minutes: that one
is about to report. A wake that composes the same block says the same. Any
ready task keeps the ordinary listing, with ready work listed first.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import plans.close_next as close_next
import plans.close_next_event as close_next_event
from prompts import plan_tools as text
from tests.plan_close_next_helpers import (
    HUMAN,
    assert_block_shape,
    assert_rows_exact,
    close,
    idle_listed,
    listed,
    next_block,
    owner_plan,
    pause,
    plan_update,
    queue_active,
    revision,
    row,
    row_ids,
    select,
    task,
)
from tests.test_plan_reconcile_release import db  # noqa: F401 - fixture

PICK_UP = "Nothing is active. If a waiting task can move now, start it."
SET_ASIDE_PICK_UP = (
    "Nothing is active. You scheduled 3 of these yourself; nothing will release them before their "
    "date unless you do. They are listed in your plan's order: start the top one you can drive now "
    "(its release_wait), or reorder them if that order is wrong."
)


def _deadline(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def _watch(
    db, plan_id: str, node_id: str, delta: timedelta, *, reason: str | None = None,
) -> dict:
    """Arm a deadline watch on one task through the boundary; return the result."""
    if row(db, node_id)["state"] == "pending":
        queue_active(db, node_id)
    params = {
        "node_id": node_id, "expected_revision": revision(db, node_id),
        "condition": {"type": "deadline", "value": _deadline(delta)},
        "expected_event": f"proof.{node_id[:8]}",
    }
    if reason is not None:
        params["reason"] = reason
        params.pop("expected_event")
    return plan_update(db, plan_id, "arm_watch", params)


def test_parking_the_last_ready_task_lists_every_wait_soonest_first_and_says_pick_one_up(db):
    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    later = task(db, plan.id, "Day-long wait")
    paused = task(db, plan.id, "Paused work")
    select(db, plan.id, current.id)
    _watch(db, plan.id, later.id, timedelta(days=1, minutes=1))
    queue_active(db, paused.id)
    pause(db, plan.id, paused.id)

    block = next_block(_watch(db, plan.id, current.id, timedelta(hours=3)))

    assert_block_shape(block, named=False)
    assert_rows_exact(db, block)
    assert block["selected"] is None
    # The task this call parked is one of the waits, so it is listed, first.
    assert row_ids(block) == [current.id, later.id, paused.id]
    assert [item["ends"] for item in block["open_tasks"]] == [
        "ends in 3 h", "ends in 24 h", None,
    ]
    assert block["open_tasks_listed"] == idle_listed(3, 3)
    assert block["idle"] == PICK_UP


def test_a_self_scheduled_wait_carries_the_exact_operation_that_resumes_it(db):
    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    select(db, plan.id, current.id)

    parked = _watch(
        db, plan.id, current.id, timedelta(hours=3), reason="After the alpha note",
    )
    block = next_block(parked)
    listed = block["open_tasks"][0]

    assert_block_shape(block, named=False)
    assert listed["release_wait"] == {
        "operation": "resolve_watch",
        "params": {
            "node_id": current.id,
            "watch_id": parked["watch"]["id"],
            "expected_revision": listed["revision"],
        },
    }
    resumed = plan_update(
        db, plan.id, listed["release_wait"]["operation"],
        listed["release_wait"]["params"],
    )
    assert resumed["admission"]["condition"] == "running"


def test_set_aside_work_leads_external_waits_in_plan_order_and_says_to_pick_it_up(db):
    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    external = task(db, plan.id, "Earlier external wait")
    first = task(db, plan.id, "First parked follow-up")
    second = task(db, plan.id, "Second parked follow-up")
    select(db, plan.id, current.id)
    _watch(db, plan.id, external.id, timedelta(hours=1))
    _watch(db, plan.id, first.id, timedelta(hours=8), reason="After first review")
    _watch(db, plan.id, second.id, timedelta(hours=6), reason="After second review")

    block = next_block(_watch(db, plan.id, current.id, timedelta(hours=12), reason="After draft"))

    assert_block_shape(block, named=False)
    assert row_ids(block) == [current.id, first.id, second.id, external.id]
    assert block["open_tasks_listed"] == idle_listed(4, 4, set_aside=3)
    assert block["idle"] == SET_ASIDE_PICK_UP
    assert all("release_wait" in item for item in block["open_tasks"][:3])


def test_set_aside_work_that_something_else_also_holds_is_not_offered_for_pick_up(db):
    from plans import holds

    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    held = task(db, plan.id, "Parked, then blocked on sign-off")
    external = task(db, plan.id, "External wait")
    select(db, plan.id, current.id)
    _watch(db, plan.id, held.id, timedelta(hours=2), reason="After review")
    holds.place_hold(
        held.id, kind="blocked", reason_key="signoff", reason="Waiting for sign-off",
        owner_kind="user", owner_id=None, expected_revision=revision(db, held.id),
        actor_id="owner", provenance="test.set_aside_and_held", db=db,
    )
    _watch(db, plan.id, external.id, timedelta(hours=1))

    block = next_block(_watch(db, plan.id, current.id, timedelta(hours=12), reason="After draft"))

    assert row_ids(block) == [current.id, external.id, held.id]
    assert block["open_tasks_listed"] == idle_listed(3, 3, set_aside=1)
    assert block["idle"] == text.idle_pick_up(1)
    assert ["release_wait" in item for item in block["open_tasks"]] == [True, False, False]


def test_a_legacy_user_hold_is_listed_to_its_agent_and_can_be_released(db):
    from plans import holds

    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    legacy = task(db, plan.id, "Old user blocker")
    select(db, plan.id, current.id)
    holds.place_hold(
        legacy.id, kind="blocked", reason_key="legacy_signoff",
        reason="Waiting for sign-off", owner_kind="user", owner_id=None,
        expected_revision=revision(db, legacy.id), actor_id="owner",
        provenance="test.legacy_user_hold", db=db,
    )
    hold_id = db.execute(
        "SELECT id FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL",
        (legacy.id,),
    ).fetchone()["id"]

    block = next_block(close(db, plan.id, "request_review", current.id))

    assert_block_shape(block, named=False)
    assert row_ids(block) == [legacy.id]
    assert block["open_tasks"][0]["wait"] == "blocked: Waiting for sign-off"
    resumed = plan_update(
        db, plan.id, "resolve_blocker", {
            "node_id": legacy.id, "hold_id": hold_id,
            "expected_revision": block["open_tasks"][0]["revision"],
        },
    )
    assert resumed["admission"]["condition"] == "ready"


def test_a_wait_ending_within_fifteen_minutes_keeps_the_list_and_drops_the_pick_up_line(db):
    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    soon = task(db, plan.id, "CI about to report")
    select(db, plan.id, current.id)
    _watch(db, plan.id, soon.id, timedelta(minutes=10))

    block = next_block(_watch(db, plan.id, current.id, timedelta(hours=5)))

    assert_block_shape(block, named=False)
    assert row_ids(block) == [soon.id, current.id]
    assert block["open_tasks"][0]["ends"] == "ends in 10 min"
    assert "release_wait" not in block["open_tasks"][0]
    assert "idle" not in block


def test_a_paused_plan_offers_no_set_aside_work_and_no_pick_up_line(db):
    from plans import service

    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    select(db, plan.id, current.id)
    _watch(db, plan.id, current.id, timedelta(hours=5), reason="After draft")
    plan_update(db, plan.id, "pause", {
        "expected_revision": service._plan_row(db, plan.id)["revision"],
    }, actor=HUMAN)

    block = close_next.executor_next(db, plan.id, "owner")

    assert block["open_tasks_listed"] == idle_listed(1, 1)
    assert "release_wait" not in block["open_tasks"][0]
    assert "idle" not in block


def test_any_ready_task_keeps_the_ordinary_listing_ready_first(db):
    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    waiting = task(db, plan.id, "Waiting work")
    ready = task(db, plan.id, "Ready work")
    select(db, plan.id, current.id)
    _watch(db, plan.id, waiting.id, timedelta(hours=2))

    block = next_block(close(db, plan.id, "request_review", current.id))

    assert_block_shape(block, named=False)
    assert row_ids(block) == [ready.id, waiting.id]
    assert block["open_tasks_listed"] == listed(2, 2)
    assert "idle" not in block
    assert all("ends" not in item for item in block["open_tasks"])


def test_a_ready_task_ranked_below_three_waits_is_still_named(db):
    """corsia#403: the three listed rows were the waits above it, in plan order."""
    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    waits = [task(db, plan.id, f"Scheduled upkeep {n}") for n in (1, 2, 3)]
    ready = task(db, plan.id, "New ready work")
    select(db, plan.id, current.id)
    for node in waits:
        _watch(db, plan.id, node.id, timedelta(hours=7))

    block = next_block(_watch(db, plan.id, current.id, timedelta(days=1)))

    assert_block_shape(block, named=False)
    assert_rows_exact(db, block)
    assert block["selected"] is None
    assert row_ids(block) == [ready.id, waits[0].id, waits[1].id]
    assert block["open_tasks_listed"] == listed(3, 4)


def test_more_than_five_waits_list_the_five_soonest_and_count_the_rest(db):
    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    waits = [task(db, plan.id, f"Wait {hours}") for hours in (9, 7, 5, 3, 8, 6)]
    select(db, plan.id, current.id)
    for node, hours in zip(waits, (9, 7, 5, 3, 8, 6)):
        _watch(db, plan.id, node.id, timedelta(hours=hours))

    block = next_block(close(db, plan.id, "request_review", current.id))

    assert_block_shape(block, named=False)
    by_hours = dict(zip((9, 7, 5, 3, 8, 6), waits))
    assert row_ids(block) == [by_hours[hours].id for hours in (3, 5, 6, 7, 8)]
    assert block["open_tasks_listed"] == idle_listed(5, 6)
    assert block["idle"] == PICK_UP


def test_a_wake_composes_the_same_idle_listing(db):
    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    select(db, plan.id, current.id)
    parked = next_block(_watch(db, plan.id, current.id, timedelta(hours=4)))

    woken = close_next.executor_next(db, plan.id, "owner")

    assert woken == parked


def test_the_close_event_records_whether_the_pick_up_line_was_said():
    base = {"named": None, "selected": None, "executor_thread_id": "owner"}
    actor = type("Actor", (), {"thread_id": "owner"})()

    def idle(block: dict) -> str:
        return close_next_event.close_event_properties(
            "arm_watch", {"next": {**base, **block}}, actor,
        )["idle"]

    assert idle({"open_tasks_listed": idle_listed(1, 1), "idle": PICK_UP}) == "pick_up"
    assert idle({"open_tasks_listed": idle_listed(1, 1, set_aside=1), "idle": text.idle_pick_up(1)}) == "pick_up"
    assert idle({"open_tasks_listed": idle_listed(1, 1)}) == "imminent"
    assert idle({"open_tasks_listed": listed(1, 1)}) == "none"


def test_ends_text_reads_in_the_coarsest_exact_unit():
    assert text.ends_text(-5) == "ended; its wake is due"
    assert text.ends_text(61) == "ends in 2 min"
    assert text.ends_text(89 * 60) == "ends in 89 min"
    assert text.ends_text(3 * 3600) == "ends in 3 h"
    assert text.ends_text(47 * 3600) == "ends in 47 h"
    assert text.ends_text(3 * 86400) == "ends in 3 days"
