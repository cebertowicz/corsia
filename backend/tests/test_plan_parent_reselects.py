"""A delegating parent takes its step back the moment its worker's wait clears.

Attaching a worker releases the parent's selection of the step: the child's
wait (``wait_causes.delegation_causes``) holds it while the worker works.
That wait clears when the worker's Turn ends, or its plan runs out of work it
can move, and neither was a write in the parent's plan, so nothing reselected
the step; the parent's next tool call restored it through the dispatch
fallback instead (#2168, 72 times in a week, every one a delegated step).

The worker's Turn end and every reconciliation of the worker's plan now
reconcile the delegator's plan too. Selecting the step is state, not news:
what the parent hears about its worker is ``_wake_parent``'s alone.
"""

from __future__ import annotations

from plans import admission, admission_attempts, closure, service
from tests.child_parent_notice_helpers import (
    begin_worker_turn,
    db,
    delegated,
    end_worker_turn,
    wakes,
)

__all__ = ["db"]


def _selected(db, node_id: str) -> bool:
    return admission_attempts.open_attempt(db, node_id) is not None


def _finish_worker_turn(db, turn_id: str = "turn-1") -> None:
    """Settle the worker's Turn row, then run its plan hook, as every harness does."""
    db.execute(
        "UPDATE active_turns SET status='completed',ended_at='2000-01-01T00:05:00' "
        "WHERE id=?",
        (turn_id,),
    )
    end_worker_turn(db)


def _wake_kinds(db) -> list[str]:
    return [str(row["id"]).split(":")[1] for row in wakes(db)]


def test_the_parent_takes_its_step_back_when_its_worker_ends_with_nothing_left(db):
    step, _plan, _node = delegated(db, with_worker_node=False)
    begin_worker_turn(db)
    assert not _selected(db, step.id)

    _finish_worker_turn(db)

    assert _selected(db, step.id)
    # The parent hears that nothing came back, once, from the one parent writer.
    assert _wake_kinds(db) == ["child_returned_nothing"]


def test_a_step_its_worker_is_still_working_stays_unselected(db):
    step, _plan, node = delegated(db)
    service.transition_node(node.id, "active", db=db)
    begin_worker_turn(db)

    _finish_worker_turn(db)

    # The worker ended its Turn with work of its own still open: still its wait.
    assert not _selected(db, step.id)


def test_the_worker_running_out_of_work_between_turns_reselects_the_parent_step(db):
    step, plan, node = delegated(db)
    service.transition_node(node.id, "active", db=db)
    assert not _selected(db, step.id)

    closure.close_node(node.id, reason="Not needed after all", db=db)
    admission.reconcile_plan(
        plan.id, actor_id=None, provenance="plan_service",
        causing_executor=None, db=db,
    )

    assert _selected(db, step.id)
    # What the parent hears about its worker is the notice matrix's alone.
    assert "work_selected" not in _wake_kinds(db)


def test_the_worker_starting_again_hands_the_parent_its_slot_back(db):
    step, plan, _node = delegated(db, with_worker_node=False)
    begin_worker_turn(db)
    _finish_worker_turn(db)
    assert _selected(db, step.id)
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    own = service.add_node(
        parent_plan.id, kind="task", title="Parent follow-up",
        acceptance_criteria="Done", db=db,
    )
    service.transition_node(own.id, "active", db=db)

    # Sent back by message: the worker takes up new work in a new Turn.
    begin_worker_turn(db, "turn-2")
    other = service.add_node(
        plan.id, kind="task", title="Another round", acceptance_criteria="Done", db=db,
    )
    service.transition_node(other.id, "active", db=db)
    admission.reconcile_plan(
        plan.id, actor_id=None, provenance="plan_service",
        causing_executor="worker", db=db,
    )

    assert not _selected(db, step.id)
    assert _selected(db, own.id)


def test_a_later_selection_by_someone_else_still_tells_the_parent(db):
    # Review flag on #3264: the worker's news is heard while the parent's slot
    # is busy; a third party then frees the slot and selects the step. That
    # selection is news the matrix never sent.
    step, _plan, _node = delegated(db, with_worker_node=False)
    begin_worker_turn(db)
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    own = service.add_node(
        parent_plan.id, kind="task", title="Parent's own task",
        acceptance_criteria="Done", db=db,
    )
    service.transition_node(own.id, "active", db=db)
    admission.reconcile_plan(
        parent_plan.id, actor_id=None, provenance="plan_service",
        causing_executor="parent", db=db,
    )
    assert _selected(db, own.id)
    _finish_worker_turn(db)
    assert not _selected(db, step.id)
    db.execute(
        "UPDATE messages SET delivery_state='delivered' "
        "WHERE thread_id='parent' AND id LIKE 'plan-wake:%'"
    )

    closure.close_node(own.id, reason="Not needed after all", db=db)
    admission.reconcile_plan(
        parent_plan.id, actor_id=None, provenance="plan_service",
        causing_executor=None, db=db,
    )

    assert _selected(db, step.id)
    assert _wake_kinds(db)[-1] == "work_selected"


def test_a_deleted_worker_hands_its_step_back_with_one_notice(db):
    from plans import delegation_child_lifecycle

    step, _plan, node = delegated(db)
    service.transition_node(node.id, "active", db=db)

    delegation_child_lifecycle.on_child_thread_ended(db, "worker", reason="deleted")

    assert _selected(db, step.id)
    assert _wake_kinds(db) == ["child_thread_ended"]


def test_a_worker_plan_write_while_its_turn_runs_leaves_the_parent_step_waiting(db):
    step, plan, _node = delegated(db, with_worker_node=False)
    begin_worker_turn(db)

    admission.reconcile_plan(
        plan.id, actor_id=None, provenance="plan_service",
        causing_executor="worker", db=db,
    )

    assert not _selected(db, step.id)
