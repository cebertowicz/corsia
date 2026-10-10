"""A change that only re-pins a selected task keeps that task in its slot.

An attempt is selected for its task's current executor and requirements
revision. A requirements edit supersedes the attempt without making the task
ineligible, so the same task is selected again at the new revision: in the
edit's own write, or when the in-flight call that deferred the release settles.
Either way a task earlier in plan order that is queued for the same executor
stays queued; plan order fills only slots that are empty.

Each scenario queues the earlier task through public plan operations: a
blocker placed and resolved while the later task holds the slot, or a review
rejected while the later task holds it.
"""

from __future__ import annotations

import pytest

from infra.database import get_db
from plans import admission, service, tool_dispatches
from tests.plan_execution_helpers import add_task
from tests.test_plan_reconcile_release import (  # noqa: F401 - shared scaffolding
    attempt,
    db,
    open_attempt,
    plan_update,
    revision,
    seed_threads,
)

NEW_DOD = "The revised requirement is verified"

_BLOCKER = {
    "reason_key": "missing_instructions",
    "reason": "A venue choice is required",
    "owner_kind": "user",
    "owner_id": "user",
}


def _node_op(db, plan_id: str, operation: str, node_id: str, **params) -> dict:
    """Run one node operation at the node's current revision."""
    return plan_update(
        db, plan_id, operation,
        {"node_id": node_id, "expected_revision": revision(db, node_id), **params},
    )


def _blocker_resolved_behind_selection(db):
    """Queue earlier Task B behind later Task A through a resolved blocker.

    Returns (plan, earlier queued task id, selected task id, its attempt id).
    """
    plan = service.ensure_plan("thread", "owner", db=db)
    earlier = add_task(db, plan.id, "Task B")
    later = add_task(db, plan.id, "Task A")
    _node_op(db, plan.id, "activate", earlier.id)
    blocked = _node_op(db, plan.id, "block_node", earlier.id, **_BLOCKER)
    selected = _node_op(db, plan.id, "activate", later.id)
    _node_op(
        db, plan.id, "resolve_blocker", earlier.id,
        hold_id=blocked["admission"]["causes"][0]["id"],
    )
    return plan, earlier.id, later.id, selected["admission"]["selection"]["attempt_id"]


def _review_rejected_behind_selection(db):
    """Queue Task A's revision behind later Task B through a rejected review.

    A's submission is rejected while B holds the slot; the executor takes up
    the revision the rejection created, which is then queued behind B.
    Returns (plan, earlier queued task id, selected task id, its attempt id).
    """
    plan = service.ensure_plan("thread", "owner", db=db)
    earlier = add_task(db, plan.id, "Task A")
    later = add_task(db, plan.id, "Task B")
    _node_op(db, plan.id, "activate", earlier.id)
    _node_op(db, plan.id, "request_review", earlier.id, evidence="Focused proof passed")
    selected = _node_op(db, plan.id, "activate", later.id)
    rejected = _node_op(
        db, plan.id, "reject", earlier.id,
        feedback="The proof misses the empty-input case",
    )
    revision = rejected["revision"]["node_id"]
    _node_op(db, plan.id, "activate", revision)
    return plan, revision, later.id, selected["admission"]["selection"]["attempt_id"]


QUEUES = {
    "blocker_resolved": _blocker_resolved_behind_selection,
    "review_rejected": _review_rejected_behind_selection,
}


def _open_dispatch(db, attempt_id: str, tool_name: str, dispatch_id: str) -> None:
    """Record one started, unsettled call of ``tool_name`` on an attempt."""
    db.execute(
        "INSERT INTO plan_tool_dispatches (id,attempt_id,turn_id,tool_name,started_at) "
        "VALUES (?,?,?,?,?)",
        (dispatch_id, attempt_id, "turn-in-flight", tool_name, service._now()),
    )
    db.commit()


def _assert_still_queued(db, earlier: str, selected: str) -> None:
    """The earlier task holds no attempt and names the selected task in its way."""
    assert open_attempt(db, earlier) is None
    projected = admission.project(earlier, db=db)
    assert projected["condition"] == "ready"
    assert projected["conflict"]["selected_node_id"] == selected


def _assert_reselected(db, node_id: str, stale_attempt: str) -> None:
    """The edited task holds a new open attempt at requirements revision 2."""
    reselected = open_attempt(db, node_id)
    assert reselected is not None, "the edited task lost its slot"
    assert reselected["id"] != stale_attempt
    assert reselected["requirements_revision"] == 2


@pytest.mark.parametrize("queue", sorted(QUEUES))
def test_requirements_edit_reselects_the_edited_task_ahead_of_earlier_queued_work(
    db, queue,
):
    plan, earlier, selected, stale_attempt = QUEUES[queue](db)

    edited = _node_op(db, plan.id, "edit", selected, acceptance_criteria=NEW_DOD)

    assert db.in_transaction is False
    assert attempt(db, stale_attempt)["release_reason"] == "requirements_changed"
    assert edited["admission"]["condition"] == "running"
    assert edited["admission"]["conflict"] is None
    _assert_reselected(db, selected, stale_attempt)
    _assert_still_queued(db, earlier, selected)


@pytest.mark.parametrize("queue", sorted(QUEUES))
def test_requirements_edit_under_an_open_call_reselects_the_edited_task_at_once(
    isolated_db, queue,
):
    """An open call does not delay the reselection (#2705); settling it changes nothing."""
    connection = get_db()
    try:
        seed_threads(connection)
        plan, earlier, selected, stale_attempt = QUEUES[queue](connection)
        _open_dispatch(connection, stale_attempt, "bash", "shell-call")
        _node_op(connection, plan.id, "edit", selected, acceptance_criteria=NEW_DOD)
        assert attempt(connection, stale_attempt)["release_reason"] == "requirements_changed"
        _assert_reselected(connection, selected, stale_attempt)
        _assert_still_queued(connection, earlier, selected)

        tool_dispatches.settle("shell-call", "succeeded")

        assert attempt(connection, stale_attempt)["release_reason"] == "requirements_changed"
        _assert_reselected(connection, selected, stale_attempt)
        _assert_still_queued(connection, earlier, selected)
    finally:
        connection.close()
