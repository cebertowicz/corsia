"""Turn plan accounting is write-once attribution, never execution authority.

A Turn records the first plan attempt it carries and keeps it. Settling that task and selecting a task on a different commitment in
the same Turn neither refuses the selection nor rewrites the snapshot.
"""

from __future__ import annotations

import sqlite3

import pytest

from infra.schema import SCHEMA_SQL
from infra.turn_plan_binding import bind_turn_to_plan_attempt
from plans import admission, service
from plans.boundary import PlanActor, mutate
from tests.plan_execution_helpers import add_task, finish_turn, open_turn, turn_accounting

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


def _mutate(db, plan_id, operation, params, key, *, turn_id=None) -> dict:
    return mutate(
        plan_id,
        operation,
        params,
        actor=PlanActor("owner", "user", None, turn_id=turn_id),
        provenance="test.turn_accounting",
        mutation_id=key,
        db=db,
    ).result


def _activate(db, plan_id, node, key) -> dict:
    return _mutate(
        db, plan_id, "activate",
        {"node_id": node.id, "expected_revision": node.revision}, key,
    )


def _settle(db, plan_id, node: dict, operation: str) -> dict:
    """Settle one selected task through the public boundary."""
    if operation == "fail":
        return _mutate(
            db, plan_id, "fail",
            {"node_id": node["id"], "expected_revision": node["revision"]},
            f"fail-{node['id']}",
        )
    reviewed = _mutate(
        db, plan_id, "request_review",
        {
            "node_id": node["id"],
            "evidence": "Focused proof passed",
            "expected_revision": node["revision"],
        },
        f"review-{node['id']}",
    )["node"]
    return _mutate(
        db, plan_id, "accept",
        {
            "node_id": reviewed["id"],
            "verification_result": "passed",
            "expected_revision": reviewed["revision"],
        },
        f"accept-{node['id']}",
        turn_id=_TURN,
    )


def test_activating_a_task_binds_an_unbound_turn(db):
    """Activating an eligible task binds a still-unbound live Turn."""
    plan = service.ensure_plan("thread", "owner", db=db)
    task = add_task(db, plan.id, "First commitment")
    open_turn(db, "owner", _TURN)

    activated = _activate(db, plan.id, task, "activate-first")

    assert turn_accounting(db, _TURN) == (
        plan.id,
        task.id,
        activated["admission"]["selection"]["attempt_id"],
        task.commitment_id,
        "execution",
    )


@pytest.mark.parametrize("settlement", ["fail", "accept"])
def test_settled_task_hands_the_same_turn_to_another_commitment(db, settlement):
    """Task A settles and Task B starts without ending the Turn."""
    plan = service.ensure_plan("thread", "owner", db=db)
    first = add_task(db, plan.id, "First commitment")
    second = add_task(db, plan.id, "Second commitment")
    assert first.commitment_id != second.commitment_id
    open_turn(db, "owner", _TURN)
    activated = _activate(db, plan.id, first, "activate-first")
    first_attempt = activated["admission"]["selection"]["attempt_id"]
    snapshot = turn_accounting(db, _TURN)

    settled = _settle(db, plan.id, activated["node"], settlement)
    continued = _activate(db, plan.id, second, "activate-second")

    assert settled["admission"]["selection"] is None
    assert continued["node"]["state"] == "active"
    assert continued["admission"]["condition"] == "running"
    assert continued["admission"]["conflict"] is None
    assert continued["admission"]["selection"]["attempt_id"] != first_attempt
    # Accounting keeps Task A: attribution only, never rewritten by B.
    assert snapshot[2] == first_attempt
    assert turn_accounting(db, _TURN) == snapshot
    # The live Turn is working B, and the display field says so.
    assert continued["admission"]["selection"]["turn_active"] is True


def test_turn_active_follows_the_executors_live_turn_not_its_accounting(db):
    """`selection.turn_active` reads the executor's live Turn, never the snapshot.

    False for a selection while no Turn is live. True while the executor's Turn
    runs, including for the same task reselected at a new requirements revision
    after the Turn's accounting was bound to its earlier attempt. False again
    once that Turn completes.
    """
    plan = service.ensure_plan("thread", "owner", db=db)
    before_turn = add_task(db, plan.id, "Selected before any Turn")
    unbound = _activate(db, plan.id, before_turn, "activate-before-turn")
    assert unbound["admission"]["selection"]["turn_active"] is False
    _mutate(
        db, plan.id, "fail",
        {"node_id": before_turn.id, "expected_revision": unbound["node"]["revision"]},
        "fail-before-turn",
    )
    in_turn = add_task(db, plan.id, "Selected inside a Turn")
    open_turn(db, "owner", _TURN)
    bound = _activate(db, plan.id, in_turn, "activate-in-turn")
    first_attempt = bound["admission"]["selection"]["attempt_id"]
    assert turn_accounting(db, _TURN)[2] == first_attempt
    assert bound["admission"]["selection"]["turn_active"] is True

    restated = _mutate(
        db, plan.id, "edit",
        {
            "node_id": in_turn.id,
            "acceptance_criteria": "The restated requirement is verified",
            "expected_revision": bound["node"]["revision"],
        },
        "edit-in-turn",
    )

    selection = restated["admission"]["selection"]
    assert selection["attempt_id"] != first_attempt
    assert turn_accounting(db, _TURN)[2] == first_attempt
    assert selection["turn_active"] is True
    finish_turn(db, _TURN)
    assert admission.project(in_turn.id, db=db)["selection"]["turn_active"] is False


def _two_selected_attempts(db):
    """Select and settle A, then select B, all before any Turn exists."""
    plan = service.ensure_plan("thread", "owner", db=db)
    first = add_task(db, plan.id, "First commitment")
    second = add_task(db, plan.id, "Second commitment")
    activated = _activate(db, plan.id, first, "activate-first")
    _settle(db, plan.id, activated["node"], "fail")
    continued = _activate(db, plan.id, second, "activate-second")
    open_turn(db, "owner", _TURN)
    return (
        plan,
        (first, activated["admission"]["selection"]["attempt_id"]),
        (second, continued["admission"]["selection"]["attempt_id"]),
    )


def _bind(db, plan, node, attempt_id):
    return bind_turn_to_plan_attempt(
        db,
        turn_id=_TURN,
        plan_id=plan.id,
        node_id=node.id,
        attempt_id=attempt_id,
        commitment_id=node.commitment_id,
        usage_role="execution",
    )


def test_binding_writer_never_rewrites_a_bound_turn(db):
    plan, (first, first_attempt), (second, second_attempt) = _two_selected_attempts(db)
    _bind(db, plan, first, first_attempt)
    snapshot = turn_accounting(db, _TURN)

    _bind(db, plan, second, second_attempt)

    assert snapshot == (
        plan.id, first.id, first_attempt, first.commitment_id, "execution",
    )
    assert turn_accounting(db, _TURN) == snapshot


def test_binding_writer_reports_whether_it_bound(db):
    """The write-once writer returns True only when it binds."""
    plan, (first, first_attempt), (second, second_attempt) = _two_selected_attempts(db)

    assert _bind(db, plan, first, first_attempt) is True
    assert _bind(db, plan, second, second_attempt) is False
    assert turn_accounting(db, _TURN)[2] == first_attempt


def _insert_usage(db, usage_id: str, binding: tuple) -> None:
    columns = "plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role"
    db.execute(
        f"""INSERT INTO llm_usage (id,thread_id,turn_id,model,source,created_at,{columns})
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (usage_id, "owner", _TURN, "test-model", "conversation", "t", *binding),
    )


def _usage_bindings(db) -> list[tuple]:
    rows = db.execute(
        """SELECT plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role
           FROM llm_usage WHERE turn_id=? ORDER BY id""",
        (_TURN,),
    ).fetchall()
    return [tuple(row) for row in rows]


def test_binding_writer_attributes_the_turns_earlier_unbound_usage(db):
    """The owner moves the earlier cost; no caller pairs a second step."""
    plan, (first, first_attempt), (second, second_attempt) = _two_selected_attempts(db)
    _insert_usage(db, "earlier-1", (None,) * 5)
    _insert_usage(db, "earlier-2", (None,) * 5)

    assert _bind(db, plan, first, first_attempt) is True

    bound = (plan.id, first.id, first_attempt, first.commitment_id, "execution")
    assert _usage_bindings(db) == [bound, bound]


def test_refused_binding_leaves_the_turns_usage_where_it_was(db):
    plan, (first, first_attempt), (second, second_attempt) = _two_selected_attempts(db)
    _bind(db, plan, first, first_attempt)
    _insert_usage(db, "after-binding", (None,) * 5)

    assert _bind(db, plan, second, second_attempt) is False

    assert _usage_bindings(db) == [(None,) * 5]
