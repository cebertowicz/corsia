"""A regroup is a change to the wait graph, and the graph's one guard judges it.

``parent_id`` is part of what work waits on: a parent finishes only after its
children, and the children inherit each of its edges to a prerequisite outside
it (``node_waits.inherited_sql``). So a move adds wait links and takes others
away, exactly as ``add_dependency`` and ``remove_dependency`` do. These cases
pin the two consequences: a move that would leave work waiting on itself is
refused where the dependency writer would refuse the same graph, and a move
reconciles the plan, as an edge change does: work it put behind an unmet
prerequisite gives up its slot, and work it freed is selected. Neither writes
an own-edge ``blocked_raised``/``blocked_cleared``: a move changes no edge of
the node's own, and ``add_dependency`` on a branch writes none for the work
beneath it either.
"""

from __future__ import annotations

import pytest

import plans.admission as admission
import plans.dependencies as dependencies
from plans import service
from plans.aggregates_recount import stored_mismatches
from tests.plan_close_next_helpers import owner_plan, plan_update, revision, row
from tests.test_plan_bulk_edit import _edits
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401 - fixtures
from tests.test_plan_regroup import _events, _move, _node, _phase


def _depends(db, node_id: str, prerequisite_id: str) -> None:
    dependencies.add_dependency(node_id, prerequisite_id, db=db)
    db.commit()


def _held(db, node_id: str) -> bool:
    return db.execute(
        "SELECT 1 FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL", (node_id,),
    ).fetchone() is not None


def _blocked_events(db, node_id: str) -> list[str]:
    return [event for (event,) in db.execute(
        "SELECT event FROM plan_node_events WHERE node_id=? AND event IN ('blocked_raised','blocked_cleared')"
        " ORDER BY created_at, rowid", (node_id,),
    ).fetchall()]


# --- a move that would wait on itself ---------------------------------------


def test_a_task_cannot_move_under_a_branch_it_waits_on(db):
    plan = owner_plan(db)
    branch = _phase(db, plan.id, "Release")
    task = _node(db, plan.id, "Announce")
    _depends(db, task.id, branch.id)
    before = revision(db, task.id)

    with pytest.raises(service.PlanError) as refused:
        _move(db, plan.id, task.id, branch.id)

    assert "Announce can't move under Release" in str(refused.value)
    assert row(db, task.id)["parent_id"] is None
    assert revision(db, task.id) == before
    assert _events(db, task.id) == []
    assert stored_mismatches(db, plan.id) == []
    # The dependency writer refuses the same graph built the other way round.
    inside = _node(db, plan.id, "Already inside", parent_id=branch.id)
    with pytest.raises(service.PlanError, match="cycle"):
        dependencies.add_dependency(inside.id, branch.id, db=db)


def test_a_branch_cannot_move_under_a_branch_it_waits_on(db):
    plan = owner_plan(db)
    target = _phase(db, plan.id, "Platform")
    moving = _phase(db, plan.id, "Rollout")
    _node(db, plan.id, "Rollout step", parent_id=moving.id)
    _depends(db, moving.id, target.id)

    with pytest.raises(service.PlanError) as refused:
        _move(db, plan.id, moving.id, target.id)

    assert "Rollout can't move under Platform" in str(refused.value)
    assert row(db, moving.id)["parent_id"] is None


def test_a_task_cannot_inherit_a_prerequisite_that_waits_on_it(db):
    plan = owner_plan(db)
    branch = _phase(db, plan.id, "Launch")
    _node(db, plan.id, "Launch step", parent_id=branch.id)
    review = _node(db, plan.id, "Review")
    task = _node(db, plan.id, "Draft")
    # Launch waits on Review, and Review waits on Draft: under Launch, Draft would
    # inherit Launch's wait on Review and so wait on itself.
    _depends(db, branch.id, review.id)
    _depends(db, review.id, task.id)

    with pytest.raises(service.PlanError) as refused:
        _move(db, plan.id, task.id, branch.id)

    assert "Draft can't move under Launch" in str(refused.value)
    assert row(db, task.id)["parent_id"] is None


def test_a_wait_the_old_branch_gave_does_not_hold_the_move(db):
    plan = owner_plan(db)
    target = _phase(db, plan.id, "Target")
    _node(db, plan.id, "Target step", parent_id=target.id)
    old = _phase(db, plan.id, "Old branch")
    task = _node(db, plan.id, "Moving", parent_id=old.id)
    _node(db, plan.id, "Stays behind", parent_id=old.id)
    # Under Old branch the task inherits its wait on Target; that link goes with the move.
    _depends(db, old.id, target.id)

    _move(db, plan.id, task.id, target.id)

    assert row(db, task.id)["parent_id"] == target.id
    assert stored_mismatches(db, plan.id) == []


def test_a_branch_that_waits_on_its_own_step_still_takes_new_work(db):
    plan = owner_plan(db)
    branch = _phase(db, plan.id, "Ship")
    step = _node(db, plan.id, "Final check", parent_id=branch.id)
    # A branch's edge to its own step is never inherited (it restates the order).
    _depends(db, branch.id, step.id)
    task = _node(db, plan.id, "Loose end")

    _move(db, plan.id, task.id, branch.id)

    assert row(db, task.id)["parent_id"] == branch.id


def test_a_bulk_regroup_refuses_only_the_row_that_would_wait_on_itself(db):
    plan = owner_plan(db)
    branch = _phase(db, plan.id, "Launch")
    waits = _node(db, plan.id, "Waits on Launch")
    free = _node(db, plan.id, "Free row")
    _depends(db, waits.id, branch.id)

    result = _edits(db, plan.id, [waits.id, free.id], parent_id=branch.id)

    assert [entry["node_id"] for entry in result["changed"]] == [free.id]
    assert "can't move under Launch" in {
        entry["node_id"]: entry["reason"] for entry in result["refused"]
    }[waits.id]
    assert row(db, waits.id)["parent_id"] is None
    assert row(db, free.id)["parent_id"] == branch.id
    assert stored_mismatches(db, plan.id) == []


# --- what a move blocked or freed -------------------------------------------


def test_moving_a_working_task_under_a_waiting_branch_blocks_it_and_frees_its_slot(db):
    plan = owner_plan(db)
    prerequisite = _node(db, plan.id, "Prerequisite")
    branch = _phase(db, plan.id, "Held branch")
    _node(db, plan.id, "Held step", parent_id=branch.id)
    _depends(db, branch.id, prerequisite.id)
    task = _node(db, plan.id, "Working")
    plan_update(db, plan.id, "activate", {"node_id": task.id, "expected_revision": revision(db, task.id)})
    assert _held(db, task.id)

    _move(db, plan.id, task.id, branch.id)

    assert admission.project(task.id, db=db)["condition"] == "blocked"
    assert not _held(db, task.id)
    assert _blocked_events(db, task.id) == []


def test_moving_a_working_task_out_of_a_waiting_branch_gives_it_its_slot_back(db):
    plan = owner_plan(db)
    # The person's step: the agent can't take it, so its own work has to wait.
    prerequisite = _node(db, plan.id, "Prerequisite", assignee="user")
    branch = _phase(db, plan.id, "Held branch")
    task = _node(db, plan.id, "Working", parent_id=branch.id)
    _node(db, plan.id, "Held sibling", parent_id=branch.id)
    plan_update(db, plan.id, "activate", {"node_id": task.id, "expected_revision": revision(db, task.id)})
    _depends(db, branch.id, prerequisite.id)
    assert admission.project(task.id, db=db)["condition"] == "blocked"
    assert not _held(db, task.id)

    _move(db, plan.id, task.id, None)

    # Selected in the same write that freed it, not at some unrelated later write.
    assert admission.project(task.id, db=db)["condition"] != "blocked"
    assert _held(db, task.id)
    assert _blocked_events(db, task.id) == []
