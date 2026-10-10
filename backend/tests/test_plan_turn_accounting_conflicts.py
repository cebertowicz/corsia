"""Selection conflicts are slot occupancy only; Turn accounting is not one.

With the executor's Turn still bound to a settled
commitment, a ready task on another commitment reports no conflict in the write
path or in either projection path, and activating it in the same Turn succeeds.
The conflict that remains is a genuinely occupied executor slot, which still
leaves the activated task pending and names the selected task.
"""

from __future__ import annotations

import sqlite3

import pytest

from infra.schema import SCHEMA_SQL
from plans import admission, admission_attempts, service
from plans.boundary import PlanActor, mutate
from tests.plan_execution_helpers import add_task, open_turn

_TURN = "turn-accounting"


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('owner','user','test/model','t','t')"
    )
    connection.commit()
    yield connection
    connection.close()


def _mutate(db, plan_id, operation, params, key) -> dict:
    return mutate(
        plan_id,
        operation,
        params,
        actor=PlanActor("owner", "user", None),
        provenance="test.turn_accounting_conflicts",
        mutation_id=key,
        db=db,
    ).result


def _activate(db, plan_id, node, key) -> dict:
    return _mutate(
        db, plan_id, "activate",
        {"node_id": node.id, "expected_revision": node.revision}, key,
    )


def _turn_commitment(db) -> str | None:
    return db.execute(
        "SELECT plan_commitment_id FROM active_turns WHERE id=?", (_TURN,),
    ).fetchone()["plan_commitment_id"]


def test_ready_task_has_no_conflict_while_turn_is_bound_to_another_commitment(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    first = add_task(db, plan.id, "First commitment")
    second = add_task(db, plan.id, "Second commitment")
    open_turn(db, "owner", _TURN)
    activated = _activate(db, plan.id, first, "activate-first")
    _mutate(
        db, plan.id, "fail",
        {"node_id": first.id, "expected_revision": activated["node"]["revision"]},
        "fail-first",
    )
    assert _turn_commitment(db) == first.commitment_id != second.commitment_id

    row = service._node_row(db, second.id)
    plan_row = service._plan_row(db, plan.id)
    write_path = admission_attempts.selection_conflict(db, row, "owner")
    resolved_path = admission.selection_conflict(db, row)
    single = admission.project(second.id, db=db)
    batched = admission.project_many([(row, plan_row)], db=db)[second.id]

    assert write_path is None
    assert resolved_path is None
    assert single["condition"] == "ready"
    assert single["conflict"] is None
    assert batched["conflict"] is None

    continued = _activate(db, plan.id, second, "activate-second")

    assert continued["admission"]["condition"] == "running"
    assert continued["admission"]["conflict"] is None
    assert _turn_commitment(db) == first.commitment_id


def test_an_occupied_executor_slot_is_still_the_selection_conflict(db):
    """An occupied executor slot is the one selection conflict."""
    plan = service.ensure_plan("thread", "owner", db=db)
    first = add_task(db, plan.id, "First commitment")
    second = add_task(db, plan.id, "Second commitment")
    open_turn(db, "owner", _TURN)
    _activate(db, plan.id, first, "activate-first")
    expected = {
        "condition": "executor_selected",
        "selected_node_id": first.id,
        "selected_node_title": "First commitment",
        "selected_node_state": "active",
    }

    row = service._node_row(db, second.id)
    write_path = admission_attempts.selection_conflict(db, row, "owner")
    refused = _activate(db, plan.id, second, "activate-second")

    assert write_path == expected
    assert refused["node"]["state"] == "pending"
    assert refused["admission"]["condition"] == "ready"
    assert refused["admission"]["conflict"] == expected
    assert admission.project(second.id, db=db)["conflict"] == expected
