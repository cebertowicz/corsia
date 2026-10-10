"""Explicit `start` and `activate` while a deferred release holds a stale attempt.

Reconciliation and settlement own the handoff, but `activate` and `start` stay
explicit selects. While an already-started non-plan call keeps Task A's
now-invalid attempt open, that attempt still occupies its executor's slot:
`start` of queued Task B and `activate` of pending Task C both return A as the
`executor_selected` conflict and select nothing. `start` of a task whose
attempt is superseded under the call - its requirements changed, or the
attempt is one a child left in its parent's plan - is not ready and opens no
second attempt. When the call settles, A is released as 'dispatch_settled' and
the plan reconciles in the same write: active B is selected with no `start`,
and pending C stays pending (nothing activates pending work).
"""

from __future__ import annotations

import pytest

from infra.database import get_db
from plans import admission, admission_attempts, projections, service, tool_dispatches
from plans.boundary import mutate
from tests.plan_execution_helpers import add_task
from tests.test_plan_reconcile_inflight import DISPATCH_ID, dispatch_row, start_dispatch
from tests.test_plan_reconcile_release import (
    ACTOR,
    attempt,
    open_attempt,
    plan_update,
    queued_pair,
    revision,
    seed_threads,
)


def _explicit(db, plan_id: str, operation: str, node_id: str) -> dict:
    """Run one explicit `start` or `activate` at the node's current revision."""
    return plan_update(
        db, plan_id, operation,
        {"node_id": node_id, "expected_revision": revision(db, node_id)},
    )


def _attempt_count(db, node_id: str) -> int:
    """Return how many attempts, open or released, a node has ever held."""
    return db.execute(
        "SELECT COUNT(*) FROM plan_node_attempts WHERE node_id=?", (node_id,),
    ).fetchone()[0]


def test_explicit_selects_never_name_a_parked_task_with_an_open_call(isolated_db):
    """A parked task's open call never makes it the conflict an explicit select names (#2705)."""
    connection = get_db()
    try:
        seed_threads(connection)
        plan, task_a, a_attempt, task_b = queued_pair(connection)
        task_c = add_task(connection, plan.id, "Task C").id
        connection.commit()
        start_dispatch(connection, a_attempt)
        plan_update(
            connection, plan.id, "request_review",
            {"node_id": task_a, "evidence": "Focused proof passed",
             "expected_revision": revision(connection, task_a)},
        )
        assert attempt(connection, a_attempt)["release_reason"] == "review_requested"
        assert open_attempt(connection, task_b) is not None

        activated = _explicit(connection, plan.id, "activate", task_c)

        assert activated["admission"]["conflict"]["selected_node_id"] == task_b
        assert open_attempt(connection, task_c) is None

        tool_dispatches.settle(DISPATCH_ID, "succeeded")

        assert dispatch_row(connection)["outcome"] == "succeeded"
        assert attempt(connection, a_attempt)["release_reason"] == "review_requested"
        assert open_attempt(connection, task_b) is not None
        assert _attempt_count(connection, task_c) == 0
    finally:
        connection.close()


def _requirements_edited_under_a_call(connection) -> tuple[str, str, str]:
    """Select Task A, open a call on its attempt, then change A's acceptance criteria.

    Returns (plan id, task A id, A's superseded attempt id).
    """
    plan, task_a, a_attempt, _task_b = queued_pair(connection)
    start_dispatch(connection, a_attempt)
    mutate(
        plan.id, "edit",
        {"node_id": task_a, "acceptance_criteria": "The revised requirement is verified",
         "expected_revision": revision(connection, task_a)},
        actor=ACTOR, provenance="test.plan_reconcile", mutation_id="edit-a",
        db=connection,
    )
    return plan.id, task_a, a_attempt


def _child_attempt_left_under_a_call(connection) -> tuple[str, str, str]:
    """Leave a child's attempt, with a call open, on a legacy task in the owner's plan.

    Older data can hold such an attempt from when the task named the child. The
    plan owner runs the task now, so that attempt is superseded as it stands.
    Returns (plan id, task A id, the child's attempt id).
    """
    plan = service.ensure_plan("thread", "owner", db=connection)
    task_a = add_task(connection, plan.id, "Task A").id
    connection.execute(
        "UPDATE plan_nodes SET executor_thread_id='worker',owner_thread_id='worker' "
        "WHERE id=?",
        (task_a,),
    )
    connection.commit()
    service.transition_node(task_a, "active", db=connection)
    left_behind = admission_attempts.insert(
        connection, service._node_row(connection, task_a), "worker",
        actor_id="owner", provenance="test.legacy",
    )
    connection.commit()
    start_dispatch(connection, left_behind["id"])
    return plan.id, task_a, left_behind["id"]


#: How Task A's attempt is superseded under an open call -> (the scene, the
#: reselected attempt's field and value). The plan owner runs A either way.
_SUPERSEDED_IN_FLIGHT = {
    "requirements_edit": (
        _requirements_edited_under_a_call, ("requirements_revision", 2),
    ),
    "child_attempt_left_behind": (
        _child_attempt_left_under_a_call, ("executor_thread_id", "owner"),
    ),
}


@pytest.mark.parametrize("supersession", sorted(_SUPERSEDED_IN_FLIGHT))
def test_start_of_a_task_superseded_in_flight_runs_it_on_a_fresh_attempt(
    isolated_db, supersession,
):
    """A task whose attempt is superseded under an open call never waits for it (#2705).

    `start` leaves the task running on one fresh attempt at its current
    requirements and executor; the superseded attempt is released and keeps
    the open call attributed to it, and nothing shows a wait on that call.
    """
    supersede, (field, value) = _SUPERSEDED_IN_FLIGHT[supersession]
    connection = get_db()
    try:
        seed_threads(connection)
        plan_id, task_a, a_attempt = supersede(connection)

        _explicit(connection, plan_id, "start", task_a)
        assert connection.in_transaction is False

        running = admission.project(task_a, db=connection)
        reselected = open_attempt(connection, task_a)
        assert running["condition"] == "running"
        assert running["selection"]["attempt_id"] == reselected["id"]
        assert reselected["id"] != a_attempt
        assert reselected[field] == value
        assert attempt(connection, a_attempt)["released_at"] is not None
        row = dispatch_row(connection)
        assert (row["attempt_id"], row["settled_at"]) == (a_attempt, None)
    finally:
        connection.close()
