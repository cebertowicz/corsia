"""The Later cut an agent draws under its own tasks.

`later` is one flag on one task: auto-selection skips it, no wake of the owner
counts it (a watch the owner armed still fires), its reason rides the event, and starting the task takes it back. The
status line counts both sides of the cut, and the set-aside expiry wake names it
as the alternative to moving a date again. Every test here drives the real
mutation boundary or the real reader, so a predicate dropped from one place fails.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from plans import later_fields, node_order, projections, service, status_context
from plans.later_fields import LATER_ON_BRANCH, REASON_WITHOUT_LATER
from plans.upkeep_notice import notice_for
from tests.plan_close_next_helpers import (
    OWNER, next_block, owner_plan, pause, plan_update, queue_active, revision, row,
    select, task,
)
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401 - fixtures


def _events(db, node_id: str) -> list[dict]:
    """Every `node_later` payload on one node, oldest first."""
    return [
        json.loads(payload) for (payload,) in db.execute(
            "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event=?"
            " ORDER BY created_at",
            (node_id, later_fields.EVENT),
        ).fetchall()
    ]


def _later(db, node_id: str) -> int:
    return row(db, node_id)["later"]


def _add(db, plan_id: str, title: str, **params) -> dict:
    return plan_update(
        db, plan_id, "add",
        {"title": title, "acceptance_criteria": f"{title} is verified", "size": "unknown", **params},
    )["node"]


# --- the flag and its refusals ---------------------------------------------


def test_add_below_the_cut_arrives_there_and_says_so_in_its_echo(db):
    plan = owner_plan(db)

    node = _add(db, plan.id, "Someday", later=True)

    assert node["later"] is True
    assert _later(db, node["id"]) == 1
    # Birth is not a crossing: nothing moved across the cut, so no event lies
    # about a reason that was never given.
    assert _events(db, node["id"]) == []


def test_add_above_the_cut_is_the_default(db):
    plan = owner_plan(db)

    assert _add(db, plan.id, "Now")["later"] is False


def test_insert_below_the_cut_keeps_its_position_and_its_flag(db):
    plan = owner_plan(db)
    first = task(db, plan.id, "First")

    node = plan_update(db, plan.id, "insert", {
        "title": "Someday", "acceptance_criteria": "Verified", "size": "unknown",
        "later": True, "before_node_id": first.id,
    })["node"]

    assert node["later"] is True
    assert _later(db, node["id"]) == 1


def test_add_cannot_put_a_branch_below_the_cut(db):
    plan = owner_plan(db)

    with pytest.raises(service.PlanInputError) as refused:
        plan_update(db, plan.id, "add", {
            "kind": "phase", "title": "Later branch", "acceptance_criteria": "Verified",
            "later": True,
        })

    assert str(refused.value) == LATER_ON_BRANCH


def test_edit_cannot_put_a_branch_below_the_cut(db):
    plan = owner_plan(db)
    phase = plan_update(db, plan.id, "add", {
        "kind": "phase", "title": "Branch", "acceptance_criteria": "Verified",
    })["node"]

    with pytest.raises(service.PlanInputError) as refused:
        plan_update(db, plan.id, "edit", {
            "node_id": phase["id"], "later": True, "reason": "Not this week",
            "expected_revision": phase["revision"],
        })

    assert str(refused.value) == LATER_ON_BRANCH


def test_a_reason_without_the_cut_is_refused(db):
    plan = owner_plan(db)
    brief = task(db, plan.id, "Draft the brief")

    with pytest.raises(service.PlanInputError) as refused:
        plan_update(db, plan.id, "edit", {
            "node_id": brief.id, "reason": "Not this week",
            "expected_revision": revision(db, brief.id),
        })

    assert str(refused.value) == REASON_WITHOUT_LATER


def test_the_cut_takes_only_a_bool(db):
    plan = owner_plan(db)
    brief = task(db, plan.id, "Draft the brief")

    with pytest.raises(service.PlanInputError):
        plan_update(db, plan.id, "edit", {
            "node_id": brief.id, "later": "yes",
            "expected_revision": revision(db, brief.id),
        })


# --- the crossing and its event --------------------------------------------


def test_edit_writes_the_flag_and_records_the_reason_once(db):
    plan = owner_plan(db)
    brief = task(db, plan.id, "Draft the brief")
    before = revision(db, brief.id)

    result = plan_update(db, plan.id, "edit", {
        "node_id": brief.id, "later": True, "reason": "Steve's cut: after launch",
        "expected_revision": before,
    })

    assert result["node"]["later"] is True
    assert _later(db, brief.id) == 1
    assert _events(db, brief.id) == [{"later": True, "reason": "Steve's cut: after launch"}]
    # One edit, one revision bump, whatever else it wrote.
    assert revision(db, brief.id) == before + 1


def test_edit_without_a_reason_still_records_the_crossing(db):
    plan = owner_plan(db)
    brief = task(db, plan.id, "Draft the brief")

    plan_update(db, plan.id, "edit", {
        "node_id": brief.id, "later": True, "expected_revision": revision(db, brief.id),
    })

    assert _events(db, brief.id) == [{"later": True, "reason": None}]


def test_bringing_a_task_back_above_the_cut_is_a_crossing_too(db):
    plan = owner_plan(db)
    brief = task(db, plan.id, "Draft the brief", later=True)

    result = plan_update(db, plan.id, "edit", {
        "node_id": brief.id, "later": False, "expected_revision": revision(db, brief.id),
    })

    assert result["node"]["later"] is False
    assert _later(db, brief.id) == 0
    assert _events(db, brief.id)[-1] == {"later": False, "reason": None}


def test_asking_for_the_cut_that_is_already_set_changes_nothing(db):
    plan = owner_plan(db)
    brief = task(db, plan.id, "Draft the brief", later=True)
    before = revision(db, brief.id)

    result = plan_update(db, plan.id, "edit", {
        "node_id": brief.id, "later": True, "reason": "again",
        "expected_revision": before,
    })

    assert result["node"]["later"] is True
    assert revision(db, brief.id) == before
    assert _events(db, brief.id) == []


def test_the_cut_crosses_alongside_a_restatement_in_one_bump(db):
    plan = owner_plan(db)
    brief = task(db, plan.id, "Draft the brief")

    plan_update(db, plan.id, "edit", {
        "node_id": brief.id, "acceptance_criteria": "Published and read",
        "later": True, "reason": "After the cut",
        "expected_revision": revision(db, brief.id),
    })

    assert row(db, brief.id)["acceptance_criteria"] == "Published and read"
    assert _later(db, brief.id) == 1
    assert len(_events(db, brief.id)) == 1


def test_parking_the_slot_task_while_restating_it_keeps_it_parked(db):
    plan = owner_plan(db)
    held = task(db, plan.id, "Held work")
    other = task(db, plan.id, "Next thing")
    select(db, plan.id, held.id)
    queue_active(db, other.id)

    result = plan_update(db, plan.id, "edit", {
        "node_id": held.id, "acceptance_criteria": "Published and read",
        "later": True, "reason": "After the cut",
        "expected_revision": revision(db, held.id),
    })

    # The requirements change releases the attempt; offering the slot back is
    # auto-selection, so it must not start the task the same edit just parked.
    assert _later(db, held.id) == 1
    assert _events(db, held.id) == [{"later": True, "reason": "After the cut"}]
    assert next_block(result)["selected"]["id"] == other.id
    assert (db.execute(
        "SELECT COUNT(*) FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL",
        (held.id,),
    ).fetchone()[0]) == 0


def test_restating_parked_work_does_not_start_it(db):
    plan = owner_plan(db)
    later = task(db, plan.id, "Someday", later=True)
    queue_active(db, later.id)

    plan_update(db, plan.id, "edit", {
        "node_id": later.id, "acceptance_criteria": "Published and read",
        "expected_revision": revision(db, later.id),
    })

    assert _later(db, later.id) == 1
    assert (db.execute(
        "SELECT COUNT(*) FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL",
        (later.id,),
    ).fetchone()[0]) == 0


# --- starting takes it out of Later ----------------------------------------


def test_starting_a_later_task_clears_the_cut_and_says_why(db):
    plan = owner_plan(db)
    brief = task(db, plan.id, "Draft the brief", later=True)

    select(db, plan.id, brief.id)

    assert _later(db, brief.id) == 0
    assert _events(db, brief.id) == [{"later": False, "reason": "started"}]


def test_auto_selection_skips_later_work_and_starts_the_task_above_the_cut(db):
    from plans import admission, selection_validity

    plan = owner_plan(db)
    ready = task(db, plan.id, "Do it now")
    later = task(db, plan.id, "Someday", later=True)
    queue_active(db, ready.id)
    queue_active(db, later.id)

    # The candidate cursor every auto-selecting reader walks.
    assert list(node_order.active_task_ids(db, plan.id)) == [ready.id]
    assert selection_validity.first_eligible_active_id(
        db, service._plan_row(db, plan.id),
    ) == ready.id

    admission.reconcile_plan(plan.id, actor_id="owner", provenance="test", db=db)
    db.commit()

    holder = db.execute(
        "SELECT node_id FROM plan_node_attempts WHERE plan_id=? AND released_at IS NULL",
        (plan.id,),
    ).fetchone()
    assert str(holder["node_id"]) == ready.id
    assert _later(db, later.id) == 1


def test_an_explicit_start_of_later_work_still_works(db):
    plan = owner_plan(db)
    first = task(db, plan.id, "Held work")
    later = task(db, plan.id, "Someday", later=True)
    select(db, plan.id, first.id)

    # The cursor hides it from auto-selection only: the agent can still name it.
    assert later.id not in node_order.active_task_ids(db, plan.id)
    assert service.get_node(later.id, db=db).later is True


# --- the slot ---------------------------------------------------------------


def test_moving_the_slot_task_below_the_cut_frees_the_slot_and_reports_next(db):
    plan = owner_plan(db)
    held = task(db, plan.id, "Held work")
    other = task(db, plan.id, "Next thing")
    select(db, plan.id, held.id)
    queue_active(db, other.id)

    result = plan_update(db, plan.id, "edit", {
        "node_id": held.id, "later": True, "reason": "After the cut",
        "expected_revision": revision(db, held.id),
    })

    assert _later(db, held.id) == 1
    assert next_block(result)["selected"]["id"] == other.id
    # The parked task stays open: it is counted in Later, not left out as if closed.
    assert next_block(result)["open_tasks_listed"].endswith(" · 1 in Later")
    assert (db.execute(
        "SELECT COUNT(*) FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL",
        (held.id,),
    ).fetchone()[0]) == 0


def test_parking_the_only_task_counts_it_in_later(db):
    from tests.plan_close_next_helpers import listed

    plan = owner_plan(db)
    held = task(db, plan.id, "Held work")
    select(db, plan.id, held.id)

    result = plan_update(db, plan.id, "edit", {
        "node_id": held.id, "later": True, "expected_revision": revision(db, held.id),
    })

    assert next_block(result)["open_tasks_listed"] == f"{listed(0, 0)} · 1 in Later"


def test_a_later_flip_on_a_task_that_holds_no_slot_reports_no_next(db):
    plan = owner_plan(db)
    first = task(db, plan.id, "Held work")
    second = task(db, plan.id, "Queued work")
    select(db, plan.id, first.id)

    result = plan_update(db, plan.id, "edit", {
        "node_id": second.id, "later": True, "expected_revision": revision(db, second.id),
    })

    assert "next" not in result


def test_bringing_a_task_back_does_not_touch_any_slot(db):
    plan = owner_plan(db)
    held = task(db, plan.id, "Held work")
    later = task(db, plan.id, "Someday", later=True)
    attempt = select(db, plan.id, held.id)

    result = plan_update(db, plan.id, "edit", {
        "node_id": later.id, "later": False, "expected_revision": revision(db, later.id),
    })

    assert "next" not in result
    assert (db.execute(
        "SELECT COUNT(*) FROM plan_node_attempts WHERE id=? AND released_at IS NULL",
        (attempt,),
    ).fetchone()[0]) == 1


# --- wakes and nudges -------------------------------------------------------


def test_a_later_task_is_never_actionable_leaf_work(db):
    plan = owner_plan(db)
    brief = task(db, plan.id, "Draft the brief")
    assert projections.liveness_signal("owner", db=db).count == 1

    plan_update(db, plan.id, "edit", {
        "node_id": brief.id, "later": True, "expected_revision": revision(db, brief.id),
    })
    db.commit()

    # Without the exclusion an unheld Later task reads `ready` and wakes its
    # owner on the 180-second liveness cadence.
    assert projections.liveness_signal("owner", db=db).count == 0


def test_the_idle_listing_lists_only_above_cut_work_then_counts_the_pile(db):
    from tests.plan_close_next_helpers import close, idle_listed, row_ids

    plan = owner_plan(db)
    active = task(db, plan.id, "In the slot")
    held = task(db, plan.id, "Waiting on legal")
    later = task(db, plan.id, "Someday", later=True)
    select(db, plan.id, active.id)
    queue_active(db, held.id)
    pause(db, plan.id, held.id)
    db.commit()

    result = close(db, plan.id, "mark_done", active.id)

    block = next_block(result)
    assert row_ids(block) == [held.id]
    assert later.id not in row_ids(block)
    assert block["open_tasks_listed"] == f"{idle_listed(1, 1)} · 1 in Later"


def test_a_busy_executors_listing_leaves_later_work_out_and_counts_it(db):
    from tests.plan_close_next_helpers import close, listed, row_ids

    plan = owner_plan(db)
    active = task(db, plan.id, "In the slot")
    following = task(db, plan.id, "Ready next")
    waiting = task(db, plan.id, "Waiting on legal")
    later = task(db, plan.id, "Someday", later=True)
    select(db, plan.id, active.id)
    queue_active(db, following.id)
    queue_active(db, waiting.id)
    pause(db, plan.id, waiting.id)
    db.commit()

    block = next_block(close(db, plan.id, "mark_done", active.id))

    assert block["selected"]["id"] == following.id
    assert row_ids(block) == [waiting.id]
    assert block["open_tasks_listed"] == f"{listed(1, 1)} · 1 in Later"


def test_when_only_later_work_is_left_the_listing_only_counts_it(db):
    from tests.plan_close_next_helpers import close, listed, row_ids

    plan = owner_plan(db)
    active = task(db, plan.id, "In the slot")
    later = task(db, plan.id, "Someday", later=True)
    select(db, plan.id, active.id)
    db.commit()

    block = next_block(close(db, plan.id, "mark_done", active.id))

    assert later.id not in row_ids(block)
    assert block["open_tasks_listed"] == f"{listed(0, 0)} · 1 in Later"


@pytest.mark.parametrize("parked", ["ready", "blocked"])
def test_a_parked_leaf_does_not_hide_that_the_rest_needs_the_person(parked):
    from plans import operator_node_state, operator_plan_display
    from tests.test_plan_not_moving import _leaf

    agent_hold = {"placed": True, "kind": "blocked", "owner_kind": "thread", "owner_id": "agent"}
    causes = (agent_hold,) if parked == "blocked" else ()
    nodes = [_leaf("theirs", "review", review="human_review"), _leaf("someday", parked, *causes)]
    counts = dict.fromkeys(operator_node_state.DISTRIBUTION_KEYS, 0)
    for node in nodes:
        counts[operator_node_state.leaf_bucket(node)] += 1

    display = operator_plan_display.plan_display(
        {"state": "active"}, counts, nodes, frozenset(), frozenset({"someday"}),
    )

    assert display["not_moving"]["need_you"] == 1


def test_the_status_line_counts_both_sides_of_the_cut(db):
    plan = owner_plan(db)
    task(db, plan.id, "Do it now")
    task(db, plan.id, "Someday", later=True)

    thin = status_context.projection(db, service._plan_row(db, plan.id), actor=OWNER)

    assert thin["progress"]["above_cut"] == 1
    assert thin["progress"]["later"] == 1


def test_the_status_never_offers_later_work_as_the_next_step(db):
    plan = owner_plan(db)
    later = task(db, plan.id, "Someday", later=True)
    following = task(db, plan.id, "Ready next")
    db.commit()

    thin = status_context.projection(db, service._plan_row(db, plan.id), actor=OWNER)

    # The Later task ranks first, so without the cut it would be offered here.
    assert thin["next_step"]["id"] == following.id
    assert later.id != thin["next_step"]["id"]


def test_the_status_counts_move_when_the_cut_moves(db):
    plan = owner_plan(db)
    brief = task(db, plan.id, "Draft the brief")

    plan_update(db, plan.id, "edit", {
        "node_id": brief.id, "later": True, "expected_revision": revision(db, brief.id),
    })

    thin = status_context.projection(db, service._plan_row(db, plan.id), actor=OWNER)

    assert (thin["progress"]["above_cut"], thin["progress"]["later"]) == (0, 1)


def test_work_the_agent_scheduled_is_only_said_when_it_is_above_the_cut(db):
    plan = owner_plan(db)
    brief = task(db, plan.id, "Draft the brief")
    due = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    plan_update(db, plan.id, "arm_watch", {
        "node_id": brief.id, "condition": {"type": "deadline", "value": due},
        "reason": "Review after the partner reads it",
        "expected_revision": revision(db, brief.id),
    })
    plan_row = service._plan_row(db, plan.id)

    said = status_context.projection(db, plan_row, actor=OWNER).get("note", "")
    assert "You scheduled 1 of these yourself" in said

    plan_update(db, plan.id, "edit", {
        "node_id": brief.id, "later": True, "expected_revision": revision(db, brief.id),
    })

    quiet = status_context.projection(db, plan_row, actor=OWNER)
    assert "You scheduled" not in quiet.get("note", "")


def test_the_upkeep_line_names_the_task_above_the_cut_and_stays_quiet_about_later(db):
    plan = owner_plan(db)
    later = task(db, plan.id, "Someday", later=True)
    ready = task(db, plan.id, "Do it now")

    line = notice_for(db, "owner", "bash")

    assert ready.id in line and later.id not in line


def test_the_upkeep_line_counts_the_later_pile_when_it_is_all_that_is_left(db):
    plan = owner_plan(db)
    task(db, plan.id, "Someday", later=True)
    task(db, plan.id, "Someday too", later=True)

    line = notice_for(db, "owner", "bash")

    assert line.endswith("plan_update add, then start it. · 2 in Later")


def test_the_upkeep_line_says_nothing_about_an_empty_pile(db):
    owner_plan(db)

    assert notice_for(db, "owner", "bash").endswith("plan_update add, then start it.")
