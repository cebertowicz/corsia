"""#2373: a step its delegated child is working reads in progress everywhere.

The pane, the status read and the digest share one predicate
(``wait_causes.child_is_working``). A watch the parent arms on the step is a
check-in, not a stop, so it does not hide the child's work; a hold, a
dependency or a decision still does.
"""

from __future__ import annotations

import pytest
import uuid

from infra.input_admission import accept_thread_input
from plans import admission, holds, selection_validity, service, status, watch_operations
from plans.boundary import PlanActor, mutate
from tests.child_parent_notice_helpers import DEADLINE, begin_worker_turn, db, delegated
from tests.plan_review_helpers import submit_review_evidence

__all__ = ["db"]

PARENT = PlanActor("parent", "user-1", "project-1")


def _read(db) -> dict:
    return status.read(db, service._plan_row(db, service.ensure_plan(
        "thread", "parent", db=db,
    ).id), actor=PARENT)


def _row(read: dict, node_id: str) -> dict:
    return next(row for row in read["current"]["rows"] if row["id"] == node_id)


def _arm_check_in(db, step) -> None:
    watch_operations.arm(
        service.ensure_plan("thread", "parent", db=db).id,
        {
            "node_id": step.id,
            "condition": {"type": "deadline", "value": DEADLINE},
            "expected_event": "external.done",
            "expected_revision": service.get_node(step.id, db=db).revision,
        },
        PARENT,
        db,
    )


def test_a_step_its_child_is_working_reads_in_progress_in_the_status_read(db):
    step, _plan, _node = delegated(db)
    begin_worker_turn(db)

    read = _read(db)
    row = _row(read, step.id)

    assert (row["label"], row["condition"]) == ("working", "running")
    assert row["reason"] == "A delegated child is working on this step"
    assert read["severity"] == "moving"
    assert read["current_task"] == {
        "id": step.id,
        "title": "Delegate work",
        "state": "active",
        "revision": service.get_node(step.id, db=db).revision,
        "condition": "running",
        "reason": "A delegated child is working on this step",
        "detail_read": f"plan_read(node_id='{step.id}')",
        "worker": {
            "thread_id": "worker",
            "plan_read": "plan_read(thread_id='worker')",
        },
    }
    assert read["no_current_task"] is False


def test_the_parents_own_selected_task_wins_over_delegated_current_work(db):
    step, _worker_plan, _node = delegated(db)
    begin_worker_turn(db)
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    own = service.add_node(
        parent_plan.id,
        kind="task",
        title="Parent follow-up",
        acceptance_criteria="Follow-up is done",
        db=db,
    )
    service.transition_node(own.id, "active", actor_id="parent", db=db)
    admission.release(step.id, "delegated", db=db)
    selected, conflict = admission.select(
        own.id,
        actor_id="parent",
        provenance="test.delegated_current",
        db=db,
        touch=False,
    )
    assert selected is not None and conflict is None
    assert selection_validity.attributed_selection(db, "parent")["node_id"] == own.id

    read = _read(db)

    assert read["current_task"]["id"] == own.id
    assert read["current_task"]["condition"] == "running"
    assert "worker" not in read["current_task"]
    assert _row(read, step.id)["label"] == "working"


def test_a_check_in_watch_does_not_hide_the_childs_work(db):
    """The reproduction: a deadline watch on the delegated step, child working."""
    step, _plan, _node = delegated(db)
    begin_worker_turn(db)
    _arm_check_in(db, step)

    read = _read(db)
    row = _row(read, step.id)

    assert (row["label"], row["condition"]) == ("working", "running")
    assert row["reason"] == "A delegated child is working on this step"
    assert read["severity"] == "moving"


def _review_only_child(db):
    step, worker_plan, child_step = delegated(db)
    service.transition_node(child_step.id, "active", actor_id="worker", db=db)
    submit_review_evidence(child_step.id, db=db, actor_id="worker")
    admission.release(child_step.id, "review_requested", db=db)
    follow_up = service.add_node(
        worker_plan.id,
        kind="task",
        title="Open the PR",
        acceptance_criteria="PR is open",
        db=db,
    )
    service.add_dependency(follow_up.id, child_step.id, db=db)
    return step


def test_a_review_only_child_does_not_read_working_because_its_turn_is_still_open(db):
    step = _review_only_child(db)
    begin_worker_turn(db)

    read = _read(db)
    row = _row(read, step.id)

    assert (row["label"], row["condition"]) == ("waiting", "waiting")
    assert row["reason"] == "The delegated child has work awaiting review"
    assert row["worker"] == {
        "thread_id": "worker",
        "plan_read": "plan_read(thread_id='worker')",
    }
    assert read["current_task"] is None
    assert read["no_current_task"] is True
    assert read["severity"] == "not_moving"


@pytest.mark.parametrize("status_value", ["running", "waiting_retry"])
def test_a_turn_begun_after_review_makes_a_review_only_child_active(db, status_value):
    """A Turn waiting to retry is as live as a running one, before or after review."""
    step = _review_only_child(db)
    begin_worker_turn(db, started_at="2999-01-01T00:00:00")
    db.execute("UPDATE active_turns SET status=?, retry_at=? WHERE thread_id='worker'",
               (status_value, "2030-01-01" if status_value == "waiting_retry" else None))

    row = _row(_read(db), step.id)

    assert row["reason"] == "A delegated child is working on this step"
    with pytest.raises(service.PlanError, match="still working"):
        _mark_done(db, step)


def test_a_queued_handback_still_makes_a_review_only_child_active(db):
    step = _review_only_child(db)
    accept_thread_input(
        "worker",
        role="user",
        content="Revise the reviewed step",
        intent="apply_now",
        message_id="revision-handback",
        db=db,
    )

    read = _read(db)
    row = _row(read, step.id)

    assert (row["label"], row["condition"]) == ("working", "running")
    assert row["reason"] == "A delegated child is working on this step"
    assert read["current_task"]["id"] == step.id


def test_a_hold_on_the_step_still_wins_over_a_busy_child(db):
    step, _plan, _node = delegated(db)
    begin_worker_turn(db)
    holds.pause(
        step.id, reason="Set aside",
        expected_revision=service.get_node(step.id, db=db).revision,
        actor_id="parent", provenance="test", db=db,
    )

    row = _row(_read(db), step.id)

    assert row["label"] == "waiting"
    assert row["condition"] == "paused"


def _active_child(db):
    step, plan, child = delegated(db)
    service.transition_node(child.id, "active", actor_id="worker", db=db)
    selected, conflict = admission.select(child.id, actor_id="worker", provenance="test", db=db)
    assert selected is not None and conflict is None
    return step, plan, child


def _mark_done(db, step):
    plan = service.ensure_plan("thread", "parent", db=db)
    return mutate(plan.id, "mark_done", {
        "node_id": step.id, "verification_result": "passed",
        "evidence": "Parent verified the work",
        "expected_revision": service.get_node(step.id, db=db).revision,
    }, actor=PARENT, provenance="test", mutation_id=uuid.uuid4().hex, db=db)


def test_stopped_selected_child_does_not_block_parent_verdict(db):
    step, _plan, child = _active_child(db)
    begin_worker_turn(db)
    db.execute("UPDATE active_turns SET status='cancelled' WHERE thread_id='worker'")
    row = _row(_read(db), step.id)
    assert row["reason"] != "A delegated child is working on this step"
    _mark_done(db, step)
    assert service.get_node(step.id, db=db).state == "accepted"
    assert service.get_node(child.id, db=db).state == "closed"


@pytest.mark.parametrize("activity", ["running", "waiting_retry", "queued"])
def test_selected_child_with_live_activity_blocks_parent_verdict(db, activity):
    step, _plan, _child = _active_child(db)
    if activity in ("running", "waiting_retry"):
        begin_worker_turn(db)
        if activity == "waiting_retry":
            db.execute("UPDATE active_turns SET status='waiting_retry', retry_at='2030-01-01' "
                       "WHERE thread_id='worker'")
    else:
        accept_thread_input("worker", role="user", content="Continue", intent="apply_now",
                            message_id="queued-work", db=db)
    with pytest.raises(service.PlanError, match="still working"):
        _mark_done(db, step)


def test_a_child_between_turns_waits_rather_than_reads_in_progress(db):
    """Open steps it has not taken up hold the step, but nobody is working it."""
    step, _plan, _node = delegated(db)
    _arm_check_in(db, step)

    row = _row(_read(db), step.id)

    assert (row["label"], row["condition"]) == ("waiting", "waiting")
