"""Only a plan's owner runs its tasks; no agent can make another thread run one.

A delegated step belongs to the subagent, which builds the sub-steps in its own
plan; the parent's step stands for the child's plan. So nothing an agent sends
assigns a task in one thread's plan to another thread, and there is no
reassign operation. A legacy task row that still names another thread is run
by the plan owner, and an attempt a child still holds in its parent's plan is
not a selection, so a thread holds at most one selection: the one in its own
plan.
"""

from __future__ import annotations

import json
import uuid

import pytest

from plans import admission_attempts, selection_validity, service
from plans.boundary import PlanActor, mutate
from tests.plan_execution_helpers import add_task
from tests.test_plan_reconcile_release import db, open_attempt, plan_update  # noqa: F401
from tools.plans import PlanUpdateTool

WORKER = PlanActor("worker", "user", None)


@pytest.mark.parametrize("operation", ["add", "insert"])
@pytest.mark.parametrize("field", ["executor_thread_id", "owner_thread_id"])
def test_no_operation_assigns_a_task_to_another_thread(db, operation, field):
    plan = service.ensure_plan("thread", "owner", db=db)
    db.commit()
    params = {
        "kind": "task", "title": "Delegated work",
        "acceptance_criteria": "Delegated work is verified", field: "worker",
    }

    with pytest.raises(
        service.PlanInputError, match=f"unexpected parameters for {operation}: {field}",
    ):
        plan_update(db, plan.id, operation, params)


def test_reassign_is_not_an_operation(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    task = add_task(db, plan.id, "Task A")
    db.commit()

    with pytest.raises(service.PlanInputError, match="unsupported plan operation: reassign"):
        plan_update(db, plan.id, "reassign", {
            "node_id": task.id, "executor_thread_id": "worker",
            "expected_revision": task.revision,
        })


def test_the_plan_writer_schema_offers_no_assignment_to_another_thread():
    schema = json.dumps(PlanUpdateTool().parameters)

    assert "reassign" not in schema
    assert "executor_thread_id" not in schema
    assert "owner_thread_id" not in schema


def _legacy_assigned_task(db, plan_id: str, thread_id: str):
    """Return a task whose row still names another thread, as older data can."""
    node = add_task(db, plan_id, "Legacy delegated task")
    db.execute(
        "UPDATE plan_nodes SET executor_thread_id=?,owner_thread_id=? WHERE id=?",
        (thread_id, thread_id, node.id),
    )
    db.commit()
    return service._node_row(db, node.id)


def test_a_legacy_task_naming_another_thread_is_run_by_the_plan_owner(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    legacy = _legacy_assigned_task(db, plan.id, "worker")

    result = plan_update(db, plan.id, "activate", {
        "node_id": legacy["id"], "expected_revision": legacy["revision"],
    })

    assert result["admission"]["condition"] == "running"
    assert open_attempt(db, legacy["id"])["executor_thread_id"] == "owner"


def test_a_child_attempt_left_in_its_parents_plan_is_not_a_selection(db):
    parent_plan = service.ensure_plan("thread", "owner", db=db)
    legacy = _legacy_assigned_task(db, parent_plan.id, "worker")
    service.transition_node(legacy["id"], "active", db=db)
    left_behind = admission_attempts.insert(
        db, service._node_row(db, legacy["id"]), "worker",
        actor_id="owner", provenance="test.legacy",
    )
    child_plan = service.ensure_plan("thread", "worker", db=db)
    own = add_task(db, child_plan.id, "Child's own step")
    db.commit()
    mutate(
        child_plan.id, "activate", {"node_id": own.id, "expected_revision": own.revision},
        actor=WORKER, provenance="test.single_owner", mutation_id=uuid.uuid4().hex, db=db,
    )

    assert selection_validity.valid_selections(db, [left_behind]) == []
    selected = selection_validity.attributed_selection(db, "worker")
    assert (selected["plan_id"], selected["node_id"]) == (child_plan.id, own.id)
