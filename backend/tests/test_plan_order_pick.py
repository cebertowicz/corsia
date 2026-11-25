"""The app's own pick follows plan order, as a named start does (#3160).

A task can become active without a start: a comment or review task is created
active (``feedback_nodes._create_linked_node``), and reconciliation then fills
the free slot with the first eligible active task. That pick passes no ready
work ranked above it, so the owner starts the higher work, or moves this one up
and starts it. Work above that is not ready (waiting on the person) passes.
"""

from __future__ import annotations

from plans import admission, service
from plans.turn_admission import snapshot_after_admission
from tests.plan_close_next_helpers import (
    close, open_attempt, owner_plan, plan_update, queue_active, revision, select, task,
)
from tests.test_plan_reconcile_release import db  # noqa: F401 - fixture


def _comment_task(db, plan_id: str, title: str = "Answer the comments") -> str:
    """Create a task the way a comment task is: added, made active, then reconciled."""
    node = service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria=f"{title} is done",
        actor_id=None, provenance="test.comment_task", db=db,
    )
    service.transition_node(node.id, "active", actor_id=None, provenance="test.comment_task", db=db)
    admission.reconcile_plan(plan_id, actor_id=None, provenance="test.comment_task", db=db)
    db.commit()
    return node.id


def test_a_comment_task_below_ready_work_is_not_picked(db):
    plan = owner_plan(db)
    task(db, plan.id, "Ship the release")
    comment = _comment_task(db, plan.id)

    assert open_attempt(db, comment) is None


def test_the_turn_start_does_not_pick_it_either(db):
    plan = owner_plan(db)
    task(db, plan.id, "Ship the release")
    comment = _comment_task(db, plan.id)

    snapshot_after_admission("owner", db=db)
    db.commit()

    assert open_attempt(db, comment) is None


def test_a_comment_task_below_work_waiting_on_the_person_is_picked(db):
    plan = owner_plan(db)
    task(db, plan.id, "Approve the scope", assignee="user")
    comment = _comment_task(db, plan.id)

    assert open_attempt(db, comment) is not None


def test_the_owner_moves_it_up_and_starts_it(db):
    plan = owner_plan(db)
    task(db, plan.id, "Ship the release")
    comment = _comment_task(db, plan.id)

    plan_update(db, plan.id, "reorder", {
        "node_id": comment, "expected_revision": revision(db, comment), "order_key": 0,
    })
    plan_update(db, plan.id, "start", {
        "node_id": comment, "expected_revision": revision(db, comment),
    })

    assert open_attempt(db, comment) is not None


def test_a_close_that_names_nothing_does_not_pass_ready_work_either(db):
    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    task(db, plan.id, "Ship the release")
    queued = task(db, plan.id, "Answer the comments")
    select(db, plan.id, current.id)
    queue_active(db, queued.id)

    result = close(db, plan.id, "mark_done", current.id)

    assert result["next"]["selected"] is None
    assert open_attempt(db, queued.id) is None


def test_a_comment_task_on_an_empty_plan_is_picked(db):
    plan = owner_plan(db)
    comment = _comment_task(db, plan.id)

    assert open_attempt(db, comment) is not None


def test_a_passed_over_task_still_wakes_its_idle_owner(db):
    """An owner that went idle with work queued has a stalled wake; new work re-arms it."""
    from automation import plan_liveness

    plan = owner_plan(db)
    task(db, plan.id, "Ship the release")
    plan_liveness.on_turn_complete(str(plan.owner_id), db=db)
    db.execute("UPDATE plan_liveness_wakes SET status=? WHERE thread_id=?",
               (plan_liveness.STALLED, str(plan.owner_id)))
    db.commit()

    comment = _comment_task(db, plan.id)

    assert open_attempt(db, comment) is None
    wake = plan_liveness.wake_for(str(plan.owner_id), db)
    assert wake is not None and wake["status"] == plan_liveness.ARMED
