"""Losing eligibility releases a selection and reconciles, plus shared scaffolding.

Task A holds its executor's slot and Task B is active and queued behind it for
the same executor. Every transition that makes A ineligible releases A's
attempt with its own reason and reconciles the plan inside that one mutation,
so B is selected with no `start`, next Turn, sweep, or repair. Transitions that
leave A eligible never touch its attempt.

`test_plan_reconcile_clearing.py`, `test_plan_reconcile_inflight.py` and
`test_plan_reconcile_inflight_registry.py` import the fixture and helpers here.
"""

from __future__ import annotations

import sqlite3
from typing import Any
import uuid

import pytest

from infra.schema import SCHEMA_SQL
from plans import admission, service
from plans.boundary import PlanActor, mutate
from tests.plan_ask_helpers import with_actionable_user_ask
from tests.plan_execution_helpers import add_task

#: The plan owner, which runs every task in its plan, and two of its direct
#: children, which each run only their own plan.
THREADS = (("owner", None), ("worker", "owner"), ("sibling", "owner"))
ACTOR = PlanActor("owner", "user", None)
#: Only the user resumes, and an agent pauses only to relay them (#1969).
HUMAN = PlanActor(None, "user", None, kind="user")


def seed_threads(connection) -> None:
    """Insert the plan owner and its two direct child executors, committed."""
    for thread_id, spawned_by in THREADS:
        connection.execute(
            "INSERT INTO threads (id,user_id,spawned_by,model,created_at,updated_at) "
            "VALUES (?, 'user', ?, 'test/model', 't', 't')",
            (thread_id, spawned_by),
        )
    connection.commit()


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    seed_threads(connection)
    yield connection
    connection.close()


def plan_update(db, plan_id: str, operation: str, params: dict, *, actor=ACTOR) -> dict:
    """Commit one mutation through `plans.boundary.mutate`, as the plan owner by default."""
    params = with_actionable_user_ask(db, plan_id, operation, params, actor)
    return mutate(
        plan_id, operation, params, actor=actor,
        provenance="test.plan_reconcile", mutation_id=uuid.uuid4().hex, db=db,
    ).result


def revision(db, node_id: str) -> int:
    """Return a node's current optimistic-concurrency revision."""
    return service._node_row(db, node_id)["revision"]


def open_attempt(db, node_id: str):
    """Return the node's open attempt row, or None."""
    return db.execute(
        "SELECT * FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL",
        (node_id,),
    ).fetchone()


def attempt(db, attempt_id: str):
    """Return one attempt row by id."""
    return db.execute(
        "SELECT * FROM plan_node_attempts WHERE id=?", (attempt_id,),
    ).fetchone()


def queued_pair(db):
    """Select Task A, then queue active Task B behind it in the owner's plan.

    Only a plan's owner runs its tasks, so both tasks share the owner's slot.
    Returns (plan, task A id, A's attempt id, task B id).
    """
    plan = service.ensure_plan("thread", "owner", db=db)
    task_a = add_task(db, plan.id, "Task A")
    task_b = add_task(db, plan.id, "Task B")
    selected = plan_update(
        db, plan.id, "activate",
        {"node_id": task_a.id, "expected_revision": task_a.revision},
    )
    service.transition_node(task_b.id, "active", db=db)
    assert open_attempt(db, task_b.id) is None
    return plan, task_a.id, selected["admission"]["selection"]["attempt_id"], task_b.id


_RELEASE_FIELDS: dict[str, dict[str, Any]] = {
    "pause_node": {"reason": "Operator paused the work"},
    "block_node": {
        "reason_key": "missing_instructions",
        "reason": "A venue choice is required",
        "owner_kind": "user",
        "owner_id": "user",
    },
    "add_dependency": {},
    "arm_watch": {
        "condition": {"type": "deadline", "value": "2099-01-01T00:00:00+00:00"},
        "expected_event": "proof.ready",
    },
    "request_review": {"evidence": "Focused proof passed"},
    "fail": {},
    "archive_node": {},
}

def release_actor(operation: str):
    """Who applies each transition: the owner, except a pause, which is the user's."""
    return HUMAN if operation == "pause_node" else ACTOR


#: Each ineligibility transition and the release reason its writer records.
RELEASES = [
    ("pause_node", "paused"),
    ("block_node", "blocked"),
    ("add_dependency", "dependency_blocked"),
    ("arm_watch", "watch_armed"),
    ("request_review", "review_requested"),
    ("fail", "failed"),
    ("archive_node", "archived"),
]


def _release_params(db, plan_id: str, operation: str, node_id: str) -> dict:
    """Build boundary params for one transition that makes the node ineligible."""
    params = dict(_RELEASE_FIELDS[operation])
    if operation == "add_dependency":
        params["depends_on_node_id"] = add_task(db, plan_id, "Unaccepted prerequisite").id
    params.update(node_id=node_id, expected_revision=revision(db, node_id))
    return params


@pytest.mark.parametrize(
    ("operation", "release_reason"), RELEASES, ids=[op for op, _ in RELEASES],
)
def test_losing_eligibility_releases_a_and_selects_queued_b_in_one_mutation(
    db, operation, release_reason,
):
    plan, task_a, a_attempt, task_b = queued_pair(db)

    result = plan_update(
        db, plan.id, operation, _release_params(db, plan.id, operation, task_a),
        actor=release_actor(operation),
    )

    assert db.in_transaction is False
    assert result["node"]["id"] == task_a
    assert result["admission"]["selection"] is None
    successor = open_attempt(db, task_b)
    assert successor is not None, f"{operation} left queued Task B unselected"
    assert successor["executor_thread_id"] == "owner"
    released = attempt(db, a_attempt)
    assert released["released_at"] is not None
    assert released["release_reason"] == release_reason


@pytest.mark.parametrize("field", ["title", "description"])
def test_intent_only_edit_keeps_the_selected_attempt(db, field):
    plan, task_a, a_attempt, task_b = queued_pair(db)

    edited = plan_update(
        db, plan.id, "edit",
        {
            "node_id": task_a,
            field: "Restated intent",
            "expected_revision": revision(db, task_a),
        },
    )

    assert edited["node"]["requirements_revision"] == 1
    assert edited["admission"]["selection"]["attempt_id"] == a_attempt
    assert attempt(db, a_attempt)["released_at"] is None
    assert open_attempt(db, task_b) is None


def test_inserting_an_earlier_node_keeps_the_selected_attempt(db):
    plan, task_a, a_attempt, task_b = queued_pair(db)

    inserted = plan_update(
        db, plan.id, "insert",
        {
            "kind": "task",
            "title": "Inserted earlier work",
            "acceptance_criteria": "The inserted work is verified", "size": "unknown",
            "before_node_id": task_a,
        },
    )["node"]

    assert inserted["order_key"] == 0
    assert service._node_row(db, task_a)["order_key"] == 1
    assert open_attempt(db, task_a)["id"] == a_attempt
    assert open_attempt(db, inserted["id"]) is None
    assert open_attempt(db, task_b) is None


def test_reordering_b_above_a_never_preempts_a(db):
    plan, task_a, a_attempt, task_b = queued_pair(db)

    reordered = plan_update(
        db, plan.id, "reorder",
        {"node_id": task_b, "order_key": 0, "expected_revision": revision(db, task_b)},
    )["node"]

    assert reordered["order_key"] == 0
    assert open_attempt(db, task_a)["id"] == a_attempt
    assert open_attempt(db, task_b) is None
    # B is now first in canonical order; reconcile still keeps A's valid selection.
    admission.reconcile_plan(
        plan.id, actor_id=None, provenance="test.plan_reconcile", db=db,
    )
    assert open_attempt(db, task_a)["id"] == a_attempt
    assert open_attempt(db, task_b) is None
