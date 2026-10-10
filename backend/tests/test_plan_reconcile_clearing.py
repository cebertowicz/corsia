"""Regained eligibility reconciles the plan in canonical order.

Removing a dependency, clearing a hold, or resolving a watch reconciles the
whole plan instead of reselecting only the node it cleared. With the executor's
slot empty, the first eligible task in canonical depth-first plan order (parent
before children, siblings by order_key then id) is selected; while a valid
selection exists nothing is preempted.
"""

from __future__ import annotations

import pytest

from plans import admission, service
from tests.plan_execution_helpers import add_task, agent_pause
from tests.test_plan_reconcile_release import (  # noqa: F401 - shared scaffolding
    HUMAN,
    db,
    open_attempt,
    plan_update,
    revision,
)


def _pause(db, plan_id: str, node_id: str, _prerequisite_id: str) -> str:
    result = agent_pause(db, node_id, "Operator paused the work")
    return result["admission"]["causes"][0]["id"]


def _resume(db, plan_id: str, node_id: str, hold_id: str) -> dict:
    return plan_update(
        db, plan_id, "resume_node",
        {"node_id": node_id, "hold_id": hold_id,
         "expected_revision": revision(db, node_id)},
    )


def _block(db, plan_id: str, node_id: str, _prerequisite_id: str) -> str:
    result = plan_update(
        db, plan_id, "block_node",
        {"node_id": node_id, "reason_key": "missing_instructions",
         "reason": "A venue choice is required", "owner_kind": "user",
         "owner_id": "user", "expected_revision": revision(db, node_id)},
    )
    return result["admission"]["causes"][0]["id"]


def _resolve_blocker(db, plan_id: str, node_id: str, hold_id: str) -> dict:
    return plan_update(
        db, plan_id, "resolve_blocker",
        {"node_id": node_id, "hold_id": hold_id,
         "expected_revision": revision(db, node_id)},
    )


def _depend(db, plan_id: str, node_id: str, prerequisite_id: str) -> str:
    plan_update(
        db, plan_id, "add_dependency",
        {"node_id": node_id, "depends_on_node_id": prerequisite_id,
         "expected_revision": revision(db, node_id)},
    )
    return prerequisite_id


def _remove_dependency(db, plan_id: str, node_id: str, prerequisite_id: str) -> dict:
    return plan_update(
        db, plan_id, "remove_dependency",
        {"node_id": node_id, "depends_on_node_id": prerequisite_id,
         "expected_revision": revision(db, node_id)},
    )


def _arm_watch(db, plan_id: str, node_id: str, _prerequisite_id: str) -> str:
    result = plan_update(
        db, plan_id, "arm_watch",
        {"node_id": node_id, "condition": {"type": "deadline", "value": "2099-01-01T00:00:00+00:00"},
         "expected_event": "proof.ready",
         "expected_revision": revision(db, node_id)},
    )
    return result["watch"]["id"]


def _resolve_watch(db, plan_id: str, node_id: str, watch_id: str) -> dict:
    return plan_update(
        db, plan_id, "resolve_watch",
        {"node_id": node_id, "watch_id": watch_id,
         "expected_revision": revision(db, node_id)},
    )


#: Clearing operation -> (place the cause and return its token, clear it by token).
CLEARINGS = {
    "resume_node": (_pause, _resume),
    "resolve_blocker": (_block, _resolve_blocker),
    "remove_dependency": (_depend, _remove_dependency),
    "resolve_watch": (_arm_watch, _resolve_watch),
}


def _held_task(db, plan_id: str, node_id: str, place) -> str:
    """Select a task, then make it ineligible; return the cause's clearing token."""
    plan_update(
        db, plan_id, "activate",
        {"node_id": node_id, "expected_revision": revision(db, node_id)},
    )
    prerequisite = add_task(db, plan_id, "Unaccepted prerequisite")
    return place(db, plan_id, node_id, prerequisite.id)


@pytest.mark.parametrize("clearing", sorted(CLEARINGS))
def test_clearing_with_an_empty_slot_selects_the_first_task_in_plan_order(db, clearing):
    place, clear = CLEARINGS[clearing]
    plan = service.ensure_plan("thread", "owner", db=db)
    first = add_task(db, plan.id, "First in plan order")
    target = add_task(db, plan.id, "Cleared later task")
    token = _held_task(db, plan.id, target.id, place)
    service.transition_node(first.id, "active", db=db)
    assert open_attempt(db, first.id) is None
    assert open_attempt(db, target.id) is None

    cleared = clear(db, plan.id, target.id, token)

    assert db.in_transaction is False
    assert cleared["admission"]["condition"] == "ready"
    assert cleared["admission"]["conflict"]["selected_node_id"] == first.id
    assert open_attempt(db, first.id) is not None
    assert open_attempt(db, target.id) is None


@pytest.mark.parametrize("clearing", sorted(CLEARINGS))
def test_clearing_an_earlier_task_never_preempts_the_valid_selection(db, clearing):
    place, clear = CLEARINGS[clearing]
    plan = service.ensure_plan("thread", "owner", db=db)
    target = add_task(db, plan.id, "Earlier cleared task")
    holder = add_task(db, plan.id, "Selected later task")
    token = _held_task(db, plan.id, target.id, place)
    selected = plan_update(
        db, plan.id, "activate",
        {"node_id": holder.id, "expected_revision": revision(db, holder.id)},
    )
    holder_attempt = selected["admission"]["selection"]["attempt_id"]

    cleared = clear(db, plan.id, target.id, token)

    assert cleared["admission"]["condition"] == "ready"
    assert cleared["admission"]["conflict"]["selected_node_id"] == holder.id
    assert open_attempt(db, holder.id)["id"] == holder_attempt
    assert open_attempt(db, target.id) is None


def _two_phase_plan(db):
    """Phase one (order 0) holds a2 at order 1; phase two (order 1) holds b1 at 0.

    Both leaves are active and queued with the executor's slot empty. Flat
    `order_key` order puts b1 first; depth-first plan order puts a2 first. Both
    phases are active too and precede their children in depth-first order, so
    only the task-only rule keeps them out of the executor slot.
    """
    plan = service.ensure_plan("thread", "owner", db=db)
    phase_one = service.add_node(
        plan.id, kind="phase", title="Phase one",
        acceptance_criteria="Phase one is verified", db=db,
    )
    phase_two = service.add_node(
        plan.id, kind="phase", title="Phase two",
        acceptance_criteria="Phase two is verified", db=db,
    )
    add_task(db, plan.id, "a1 not started", parent_id=phase_one.id)
    a2 = add_task(db, plan.id, "a2 queued", parent_id=phase_one.id)
    b1 = add_task(db, plan.id, "b1 queued", parent_id=phase_two.id)
    assert (phase_one.order_key, a2.order_key, phase_two.order_key, b1.order_key) == (
        0, 1, 1, 0,
    )
    for node in (phase_one, phase_two, a2, b1):
        service.transition_node(node.id, "active", db=db)
    assert open_attempt(db, a2.id) is None
    assert open_attempt(db, b1.id) is None
    return plan, (phase_one.id, phase_two.id), a2, b1


def _phases_with_an_attempt(db, phase_ids) -> list[str]:
    return [phase_id for phase_id in phase_ids if open_attempt(db, phase_id) is not None]


def test_reconcile_fills_the_empty_slot_in_depth_first_plan_order(db):
    plan, phases, a2, b1 = _two_phase_plan(db)

    admission.reconcile_plan(
        plan.id, actor_id=None, provenance="test.plan_reconcile", db=db,
    )

    assert open_attempt(db, a2.id) is not None
    assert open_attempt(db, b1.id) is None
    assert _phases_with_an_attempt(db, phases) == []


def test_plan_resume_selects_in_depth_first_plan_order(db):
    plan, phases, a2, b1 = _two_phase_plan(db)
    paused = plan_update(
        db, plan.id, "pause",
        {"expected_revision": service._plan_row(db, plan.id)["revision"]}, actor=HUMAN,
    )

    resumed = plan_update(
        db, plan.id, "resume", {"expected_revision": paused["plan"]["revision"]}, actor=HUMAN,
    )

    assert resumed["plan"]["state"] == "active"
    assert open_attempt(db, a2.id) is not None
    assert open_attempt(db, b1.id) is None
    assert _phases_with_an_attempt(db, phases) == []


def test_reconcile_never_gives_an_active_phase_an_attempt(db):
    """Only tasks take executor slots, never an active phase.

    An active phase with no live children comes first in plan order and would
    pass every other eligibility check; reconcile still selects the first task.
    """
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = service.add_node(
        plan.id, kind="phase", title="Structural phase",
        acceptance_criteria="All child tasks are complete", order_key=0, db=db,
    )
    task = add_task(db, plan.id, "First task", order_key=1)
    for node in (phase, task):
        service.transition_node(node.id, "active", db=db)

    admission.reconcile_plan(
        plan.id, actor_id=None, provenance="test.plan_reconcile", db=db,
    )

    assert open_attempt(db, phase.id) is None
    assert open_attempt(db, task.id) is not None


def test_plan_resume_reselects_queued_work_without_a_start(db):
    """Resuming a paused plan selects its owner's queued work in the same write."""
    plan = service.ensure_plan("thread", "owner", db=db)
    queued = add_task(db, plan.id, "Owner queued work")
    service.transition_node(queued.id, "active", db=db)
    paused = plan_update(
        db, plan.id, "pause",
        {"expected_revision": service._plan_row(db, plan.id)["revision"]}, actor=HUMAN,
    )
    selected_while_paused = open_attempt(db, queued.id)

    plan_update(
        db, plan.id, "resume", {"expected_revision": paused["plan"]["revision"]}, actor=HUMAN,
    )

    assert selected_while_paused is None
    assert open_attempt(db, queued.id)["executor_thread_id"] == "owner"
