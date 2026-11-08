"""An agent regroups its plan: one task moves to another branch (design §4).

A plan drifts out of shape faster than an agent can rebuild it: the work it
added under one outcome belongs under another, and rows sit at top level
because no branch existed when they were written. ``edit`` now takes
``parent_id`` (``null`` for top level), so a regroup is a list of edits rather
than a rebuild, and the task keeps its id, history, dependencies, holds, waits,
commitment and Later flag (PS2) -- everything a close-and-re-add would cost.

The two hard parts are the ones the tests aim at. Nothing guarded a node's
ancestors before (``plans.hierarchy`` is plan-level only), so a move into one's
own branch is refused here for the first time. And ``parent_id`` is not an
aggregate input ``aggregates.apply`` can see -- the moved row's own figures do
not change, only who receives them -- so both the branch it left and the branch
it joined are re-derived explicitly. Every move therefore ends with
``aggregates_recount.stored_mismatches`` empty, which is the design's oracle.
"""

from __future__ import annotations

import json

import pytest

import plans.dependencies as dependencies
from plans import service, writer_context
from plans.aggregates_recount import stored_mismatches
from plans.boundary import PlanActor
from plans.later_fields import LATER_ON_BRANCH
from tests.plan_close_next_helpers import OWNER, owner_plan, plan_update, revision, row
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401 - fixtures

#: A worker thread may move its own plan's tasks (``boundary.authorize_thread_owner``).
WORKER = PlanActor("worker", "user", None)
BRANCH_INTENT = "Deliver the branch."
TASK_DOD = "Verified by the owner"


def _phase(db, plan_id: str, title: str = "Branch", *, intent: str | None = BRANCH_INTENT,
           parent_id: str | None = None):
    return service.add_node(
        plan_id, kind="phase", title=title, acceptance_criteria=f"{title} is verified",
        intent=intent, parent_id=parent_id, db=db,
    )


def _accepted(db, node_id: str) -> None:
    """Settle a task the way its owner does, so its branch counts it as done.

    ``accepted`` needs review evidence for the current requirements, so the scene
    settles through the real Mark done rather than forcing the transition.
    """
    plan = row(db, node_id)["plan_id"]
    service.transition_node(node_id, "active", db=db)
    db.commit()
    plan_update(db, plan, "mark_done", {
        "node_id": node_id, "expected_revision": revision(db, node_id),
        "evidence": "Verified by the owner in the scene",
    })


def _node(db, plan_id: str, title: str, *, parent_id: str | None = None, **kwargs):
    return service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria=TASK_DOD,
        parent_id=parent_id, db=db, **kwargs,
    )


def _move(db, plan_id: str, node_id: str, parent_id, *, actor=OWNER, **extra) -> dict:
    """One regroup through the real mutation boundary, at the node's revision."""
    return plan_update(
        db, plan_id, "edit",
        {"node_id": node_id, "expected_revision": revision(db, node_id),
         "parent_id": parent_id, **extra},
        actor=actor,
    )


def _events(db, node_id: str) -> list[dict]:
    return [
        json.loads(payload) for (payload,) in db.execute(
            "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event=?"
            " ORDER BY created_at",
            (node_id, "node_reparented"),
        ).fetchall()
    ]


def _open(db, node_id: str) -> int:
    return row(db, node_id)["prop_open"]


# --- the move ---------------------------------------------------------------


def test_reparent_moves_a_task_under_a_phase_and_says_so(db):
    plan = owner_plan(db)
    branch = _phase(db, plan.id, "Outcomes")
    task = _node(db, plan.id, "Loose end")
    before = revision(db, task.id)

    result = _move(db, plan.id, task.id, branch.id)

    moved = row(db, task.id)
    assert moved["parent_id"] == branch.id
    assert moved["revision"] == before + 1
    assert result["node"]["parent_id"] == branch.id
    # One write, one revision bump: the figures move without a second touch.
    assert _events(db, task.id) == [{"from": None, "to": branch.id}]


def test_reparent_places_the_task_last_among_its_new_open_siblings(db):
    plan = owner_plan(db)
    branch = _phase(db, plan.id, "Outcomes")
    first = _node(db, plan.id, "First", parent_id=branch.id)
    second = _node(db, plan.id, "Second", parent_id=branch.id)
    arriving = _node(db, plan.id, "Arriving")

    _move(db, plan.id, arriving.id, branch.id)

    listed = db.execute(
        "SELECT id FROM plan_nodes WHERE plan_id=? AND parent_id=? AND archived_at IS NULL "
        "ORDER BY order_key, id",
        (plan.id, branch.id),
    ).fetchall()
    assert [str(entry["id"]) for entry in listed] == [first.id, second.id, arriving.id]


def test_reparent_to_top_level_takes_null(db):
    plan = owner_plan(db)
    branch = _phase(db, plan.id, "Outcomes")
    task = _node(db, plan.id, "Task", parent_id=branch.id)

    _move(db, plan.id, task.id, None)

    assert row(db, task.id)["parent_id"] is None
    assert _events(db, task.id) == [{"from": branch.id, "to": None}]


def test_reparent_between_branches_leaves_the_rest_of_the_node_alone(db):
    plan = owner_plan(db)
    first = _phase(db, plan.id, "First")
    second = _phase(db, plan.id, "Second")
    task = _node(db, plan.id, "Task", parent_id=first.id, later=True)
    obligation = row(db, task.id)["commitment_id"]
    prerequisite = _node(db, plan.id, "Prerequisite")
    dependencies.add_dependency(task.id, prerequisite.id, db=db)
    service._event(
        db, task.id, "node_later", actor_id=None, provenance="test",
        payload={"later": True, "reason": "after launch"},
    )
    db.commit()

    _move(db, plan.id, task.id, second.id)

    moved = row(db, task.id)
    # PS2: the move is a re-hanging, not a re-creation.
    assert moved["commitment_id"] == obligation
    assert moved["later"] == 1
    assert moved["acceptance_criteria"] == TASK_DOD
    assert db.execute(
        "SELECT 1 FROM plan_node_events WHERE node_id=? AND event='node_later'", (task.id,),
    ).fetchone() is not None
    assert db.execute(
        "SELECT 1 FROM plan_node_dependencies WHERE node_id=? AND depends_on_node_id=?",
        (task.id, prerequisite.id),
    ).fetchone() is not None


# --- the refusals -----------------------------------------------------------


def test_reparent_into_its_own_branch_is_refused(db):
    plan = owner_plan(db)
    branch = _phase(db, plan.id, "Outcomes")
    child = _phase(db, plan.id, "Step", parent_id=branch.id)

    with pytest.raises(service.PlanError) as refused:
        _move(db, plan.id, branch.id, child.id)

    assert "Outcomes can't move inside its own branch (Step is beneath it)." in str(refused.value)
    assert row(db, branch.id)["parent_id"] is None
    assert stored_mismatches(db, plan.id) == []


def test_reparent_two_levels_down_into_itself_is_refused(db):
    plan = owner_plan(db)
    outer = _phase(db, plan.id, "Outer")
    inner = _phase(db, plan.id, "Inner", parent_id=outer.id)
    deep = _phase(db, plan.id, "Deep", parent_id=inner.id)

    with pytest.raises(service.PlanError) as refused:
        _move(db, plan.id, outer.id, deep.id)

    assert "Outer can't move inside its own branch (Deep is beneath it)." in str(refused.value)
    assert row(db, outer.id)["parent_id"] is None


def test_reparent_a_node_under_itself_is_refused(db):
    plan = owner_plan(db)
    branch = _phase(db, plan.id, "Outcomes")

    with pytest.raises(service.PlanInputError) as refused:
        _move(db, plan.id, branch.id, branch.id)

    assert "a node cannot be its own parent" in str(refused.value)
    assert row(db, branch.id)["parent_id"] is None


def test_reparent_under_a_task_is_refused_with_the_phase_hint(db):
    plan = owner_plan(db)
    leaf = _node(db, plan.id, "Leaf")
    task = _node(db, plan.id, "Task")

    with pytest.raises(service.PlanInputError) as refused:
        _move(db, plan.id, task.id, leaf.id)

    assert "task nodes are leaves and cannot contain children" in str(refused.value)
    assert "make a phase with add kind='phase', then move these under it" in str(refused.value)


def test_reparent_into_another_plan_is_refused(db):
    plan = owner_plan(db)
    other = service.ensure_plan("thread", "worker", db=db)
    elsewhere = _phase(db, other.id, "Elsewhere")
    db.commit()
    task = _node(db, plan.id, "Task")

    with pytest.raises(service.PlanInputError) as refused:
        _move(db, plan.id, task.id, elsewhere.id)

    assert "parent_id must be a node in this plan." in str(refused.value)
    assert row(db, task.id)["parent_id"] is None


def test_reparent_onto_an_unknown_id_is_refused_as_the_same_rule(db):
    plan = owner_plan(db)
    task = _node(db, plan.id, "Task")

    with pytest.raises(service.PlanInputError) as refused:
        _move(db, plan.id, task.id, "no-such-node")

    assert "parent_id must be a node in this plan." in str(refused.value)


def test_reparent_into_a_finished_branch_is_refused(db):
    plan = owner_plan(db)
    branch = _phase(db, plan.id, "Done branch")
    done = _node(db, plan.id, "Finished", parent_id=branch.id)
    _accepted(db, done.id)
    db.commit()
    task = _node(db, plan.id, "Task")

    with pytest.raises(service.PlanError):
        _move(db, plan.id, task.id, done.id)

    assert row(db, task.id)["parent_id"] is None


def test_reparent_onto_the_same_parent_writes_nothing(db):
    plan = owner_plan(db)
    branch = _phase(db, plan.id, "Outcomes")
    task = _node(db, plan.id, "Task", parent_id=branch.id)
    before = revision(db, task.id)

    _move(db, plan.id, task.id, branch.id)

    assert revision(db, task.id) == before
    assert _events(db, task.id) == []


# --- the figures on both chains --------------------------------------------


def test_reparent_rederives_the_branch_it_left_and_the_one_it_joined(db):
    plan = owner_plan(db)
    left = _phase(db, plan.id, "Left")
    right = _phase(db, plan.id, "Right")
    staying = _node(db, plan.id, "Staying", parent_id=left.id)
    moving = _node(db, plan.id, "Moving", parent_id=left.id)
    assert stored_mismatches(db, plan.id) == []

    _move(db, plan.id, moving.id, right.id)

    assert stored_mismatches(db, plan.id) == []
    assert row(db, left.id)["sub_open"] == _open(db, staying.id)
    assert row(db, left.id)["child_in_work"] == 1
    assert row(db, right.id)["sub_open"] == _open(db, moving.id)
    assert row(db, right.id)["child_in_work"] == 1


def test_reparent_between_branches_moves_no_plan_figure(db):
    plan = owner_plan(db)
    left = _phase(db, plan.id, "Left")
    right = _phase(db, plan.id, "Right")
    moving = _node(db, plan.id, "Moving", parent_id=left.id)
    before = db.execute("SELECT * FROM plans WHERE id=?", (plan.id,)).fetchone()

    _move(db, plan.id, moving.id, right.id)

    after = db.execute("SELECT * FROM plans WHERE id=?", (plan.id,)).fetchone()
    assert stored_mismatches(db, plan.id) == []
    assert [after[name] for name in before.keys() if name.startswith("top_")] == [
        before[name] for name in before.keys() if name.startswith("top_")
    ]


def test_reparent_to_top_level_hands_the_figures_to_the_plan(db):
    plan = owner_plan(db)
    branch = _phase(db, plan.id, "Branch")
    moving = _node(db, plan.id, "Moving", parent_id=branch.id)
    before = db.execute("SELECT top_open, top_roots FROM plans WHERE id=?", (plan.id,)).fetchone()

    _move(db, plan.id, moving.id, None)

    after = db.execute("SELECT top_open, top_roots FROM plans WHERE id=?", (plan.id,)).fetchone()
    assert stored_mismatches(db, plan.id) == []
    # The move put an open task on the top level, so the plan gained a root and an
    # open one: the figures the branch used to carry are now the plan's.
    assert after["top_open"] == before["top_open"] + 1
    assert after["top_roots"] == before["top_roots"] + 1


def test_reparent_out_of_top_level_takes_the_figures_back(db):
    plan = owner_plan(db)
    branch = _phase(db, plan.id, "Branch")
    task = _node(db, plan.id, "Task")

    _move(db, plan.id, task.id, branch.id)

    assert stored_mismatches(db, plan.id) == []
    assert row(db, branch.id)["sub_open"] == _open(db, task.id)
    assert row(db, branch.id)["child_in_work"] == 1


def test_a_branch_that_loses_its_last_child_is_a_branch_again(db):
    """An open task under a settled parent is impossible; the parent returns as a leaf."""
    plan = owner_plan(db)
    branch = _phase(db, plan.id, "Branch")
    only = _node(db, plan.id, "Only", parent_id=branch.id)

    _move(db, plan.id, only.id, None)

    assert stored_mismatches(db, plan.id) == []
    assert row(db, branch.id)["child_in_work"] == 0
    assert row(db, branch.id)["sub_open"] == 0


def test_reparent_a_settled_task_moves_its_done_figure_between_branches(db):
    plan = owner_plan(db)
    left = _phase(db, plan.id, "Left")
    right = _phase(db, plan.id, "Right")
    done = _node(db, plan.id, "Done", parent_id=left.id)
    _accepted(db, done.id)
    db.commit()
    assert row(db, left.id)["prop_done"] == 1

    _move(db, plan.id, done.id, right.id)

    assert stored_mismatches(db, plan.id) == []
    assert row(db, left.id)["prop_done"] == 0
    assert row(db, right.id)["prop_done"] == 1


# --- the intent the branch owns --------------------------------------------


def test_reparent_rederives_an_inherited_intent_only(db):
    plan = owner_plan(db)
    first = _phase(db, plan.id, "First")
    second = _phase(db, plan.id, "Second")
    inheriting = _node(
        db, plan.id, "Inheriting", parent_id=first.id,
        intent_node_id=first.id,
    )
    owning = _node(db, plan.id, "Owning", parent_id=first.id, intent="Its own outcome.")
    own_before = revision(db, owning.id)

    _move(db, plan.id, inheriting.id, second.id)
    _move(db, plan.id, owning.id, second.id)

    assert row(db, inheriting.id)["intent_node_id"] == second.id
    assert row(db, owning.id)["intent_node_id"] is None
    assert row(db, owning.id)["intent"] == "Its own outcome."
    assert revision(db, owning.id) == own_before + 1
    assert stored_mismatches(db, plan.id) == []


def test_reparent_to_top_level_keeps_a_worker_plan_inherited_intent(db):
    """A worker plan's tasks inherit their one intent from the step above them.

    Top level in a worker plan is no branch, yet the plan-level inheritance
    still owns the intent: a move to top level must not strand the task with
    neither an intent of its own nor an intent to inherit.
    """
    worker_plan = service.ensure_plan("thread", "worker", db=db)
    step = _node(db, owner_plan(db).id, "Step")
    service.touch_node(db, step.id, spawned_thread_id="worker")
    imported = _node(db, worker_plan.id, "Inherited", intent_node_id=step.id)
    db.commit()

    _move(db, worker_plan.id, imported.id, None, actor=WORKER)

    assert stored_mismatches(db, worker_plan.id) == []
    assert row(db, imported.id)["intent_node_id"] == step.id


# --- the surface the agent reads -------------------------------------------


def test_edit_names_parent_id_with_the_cut_in_one_call(db):
    plan = owner_plan(db)
    branch = _phase(db, plan.id, "Branch")
    task = _node(db, plan.id, "Task")
    before = revision(db, task.id)

    _move(db, plan.id, task.id, branch.id, later=True, reason="after launch")

    moved = row(db, task.id)
    assert (moved["parent_id"], moved["later"]) == (branch.id, 1)
    assert moved["revision"] == before + 1
    assert _events(db, task.id) == [{"from": None, "to": branch.id}]


def test_a_branch_still_refuses_the_cut_and_now_names_node_ids(db):
    """Slice 1's wording pointed at per-task edits; slice 2 ships the bulk call."""
    plan = owner_plan(db)
    branch = _phase(db, plan.id, "Branch")

    with pytest.raises(service.PlanInputError) as refused:
        plan_update(db, plan.id, "edit", {
            "node_id": branch.id, "expected_revision": revision(db, branch.id), "later": True,
        })

    assert str(refused.value) == LATER_ON_BRANCH
    assert "node_ids" in LATER_ON_BRANCH
    assert "edit each of its open tasks" not in LATER_ON_BRANCH
