"""Executor selection and hold invariants for reliable structured plans."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from infra.schema import SCHEMA_SQL
from plans import service
from plans.boundary import PlanActor, mutate
from tests.plan_ask_helpers import with_actionable_user_ask
from tests.plan_execution_helpers import agent_pause


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO threads "
        "(id,user_id,spawned_by,model,created_at,updated_at) "
        "VALUES ('owner', 'user', NULL, 'test/model', 't', 't')",
    )
    connection.commit()
    yield connection
    connection.close()


def _actor(thread_id: str = "owner"):
    return PlanActor(thread_id, "user", None)


def _mutate(db, plan_id, operation, params, key, *, thread_id: str = "owner"):
    actor = _actor(thread_id)
    params = with_actionable_user_ask(db, plan_id, operation, params, actor)
    return mutate(
        plan_id,
        operation,
        params,
        actor=actor,
        provenance="test.plan_admission",
        mutation_id=key,
        db=db,
    ).result


def _task(db, plan_id, title):
    return service.add_node(
        plan_id,
        kind="task",
        title=title,
        acceptance_criteria=f"{title} is verified",
        db=db,
    )


def test_v7_schema_has_one_attempt_hold_dispatch_and_usage_contract(db):
    node_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(plan_nodes)")
    }
    turn_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(active_turns)")
    }
    usage_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(llm_usage)")
    }

    assert {
        "executor_thread_id", "requirements_revision", "commitment_id",
    } <= node_columns
    assert {
        "plan_id", "plan_node_id", "plan_attempt_id", "plan_commitment_id",
        "plan_usage_role",
    } <= turn_columns
    assert {
        "plan_id", "plan_node_id", "plan_attempt_id", "plan_commitment_id",
        "plan_usage_role",
    } <= usage_columns
    assert {
        "plan_node_attempts", "plan_node_holds", "plan_tool_dispatches",
    } <= {
        row["name"]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def test_activate_atomically_opens_the_only_executor_selection(db):
    """Activate is an explicit select that never takes an occupied slot.

    Activating a pending task while the executor's slot is occupied leaves it
    pending and names the selected task; reconciliation never overrides that.
    """
    plan = service.ensure_plan("thread", "owner", db=db)
    first = _task(db, plan.id, "First")
    second = _task(db, plan.id, "Second")

    selected = _mutate(
        db,
        plan.id,
        "activate",
        {"node_id": first.id, "expected_revision": first.revision},
        "activate-first",
    )
    conflicted = _mutate(
        db,
        plan.id,
        "activate",
        {"node_id": second.id, "expected_revision": second.revision},
        "activate-second",
    )

    assert selected["node"]["state"] == "active"
    assert selected["admission"]["condition"] == "running"
    selection = selected["admission"]["selection"]
    assert selection["executor_thread_id"] == "owner"
    assert selection["requirements_revision"] == 1
    assert conflicted["node"]["state"] == "pending"
    assert conflicted["admission"]["condition"] == "ready"
    assert conflicted["admission"]["conflict"] == {
        "condition": "executor_selected",
        "selected_node_id": first.id,
        "selected_node_title": "First",
        "selected_node_state": "active",
    }
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_attempts WHERE released_at IS NULL"
    ).fetchone()[0] == 1


def test_concurrent_activation_serializes_one_slot_and_other_executors_progress(tmp_path):
    path = tmp_path / "plans.db"
    setup = sqlite3.connect(path)
    setup.row_factory = sqlite3.Row
    setup.execute("PRAGMA foreign_keys=ON")
    setup.executescript(SCHEMA_SQL)
    for thread_id in ("owner", "worker"):
        setup.execute(
            "INSERT INTO threads (id,user_id,created_at,updated_at) "
            "VALUES (?,'user','t','t')",
            (thread_id,),
        )
    plan = service.ensure_plan("thread", "owner", db=setup)
    first = _task(setup, plan.id, "First concurrent")
    second = _task(setup, plan.id, "Second concurrent")
    # Only a plan's owner runs its tasks, so another executor's slot is in its own plan.
    worker_plan = service.ensure_plan("thread", "worker", db=setup)
    independent = _task(setup, worker_plan.id, "Independent executor")
    setup.commit()
    setup.close()

    barrier = threading.Barrier(3)

    def activate(node_id: str, mutation_id: str):
        # Serialization is the property, not latency: the loser waits out the
        # winner's whole write, and a shared CI runner once took 5.8s in that
        # write's commit alone (cut 5, PR #3014).
        connection = sqlite3.connect(path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        barrier.wait()
        try:
            return _mutate(
                connection,
                plan.id,
                "activate",
                {"node_id": node_id, "expected_revision": 1},
                mutation_id,
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(activate, first.id, "activate-concurrent-first"),
            pool.submit(activate, second.id, "activate-concurrent-second"),
        ]
        barrier.wait()
        results = [future.result() for future in futures]

    states = sorted(result["node"]["state"] for result in results)
    assert states == ["active", "pending"]
    conflicts = [
        result["admission"]["conflict"]
        for result in results
        if result["admission"]["conflict"] is not None
    ]
    assert len(conflicts) == 1
    assert conflicts[0]["condition"] == "executor_selected"

    check = sqlite3.connect(path)
    check.row_factory = sqlite3.Row
    independently_selected = _mutate(
        check,
        worker_plan.id,
        "activate",
        {"node_id": independent.id, "expected_revision": independent.revision},
        "activate-independent-executor",
        thread_id="worker",
    )
    assert independently_selected["admission"]["condition"] == "running"
    assert {
        row["executor_thread_id"]
        for row in check.execute(
            "SELECT executor_thread_id FROM plan_node_attempts "
            "WHERE released_at IS NULL"
        )
    } == {"owner", "worker"}
    check.close()


def test_turn_start_snapshots_the_current_selected_attempt(db):
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Turn-bound")
    selected = _mutate(
        db,
        plan.id,
        "activate",
        {"node_id": node.id, "expected_revision": node.revision},
        "activate-turn-bound",
    )
    selection = selected["admission"]["selection"]
    accept_thread_input(
        "owner",
        role="user",
        content="continue",
        intent="apply_now",
        message_id="turn-bound-input",
        db=db,
    )

    batch = start_idle_turn(
        "owner",
        turn_id="turn-bound",
        boot_epoch="test",
        started_at="t",
        db=db,
    )

    assert batch is not None
    turn = db.execute(
        """SELECT plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,
                  plan_usage_role
           FROM active_turns WHERE id='turn-bound'"""
    ).fetchone()
    assert tuple(turn) == (
        plan.id,
        node.id,
        selection["attempt_id"],
        selected["node"]["commitment_id"],
        "execution",
    )


def test_pause_and_block_use_one_safe_release_and_reacquire_path(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Held")
    active = _mutate(
        db,
        plan.id,
        "activate",
        {"node_id": node.id, "expected_revision": node.revision},
        "activate-held",
    )["node"]

    assert active["revision"] == service._node_row(db, node.id)["revision"]
    paused = agent_pause(db, node.id, "Operator paused work")
    pause_hold = paused["admission"]["causes"][0]
    assert paused["admission"]["condition"] == "paused"
    assert pause_hold["kind"] == "paused"
    assert db.execute(
        "SELECT released_at,release_reason FROM plan_node_attempts"
    ).fetchone()["release_reason"] == "paused"

    resumed = _mutate(
        db,
        plan.id,
        "resume_node",
        {
            "node_id": node.id,
            "hold_id": pause_hold["id"],
            "expected_revision": paused["node"]["revision"],
        },
        "resume-held",
    )
    assert resumed["admission"]["condition"] == "running"

    blocked = _mutate(
        db,
        plan.id,
        "block_node",
        {
            "node_id": node.id,
            "reason_key": "missing_instructions",
            "reason": "A venue choice is required",
            "owner_kind": "user",
            "owner_id": "user",
            "expected_revision": resumed["node"]["revision"],
        },
        "block-held",
    )
    block_hold = blocked["admission"]["causes"][0]
    assert blocked["admission"]["condition"] == "blocked"
    assert block_hold["reason_key"] == "missing_instructions"
    assert block_hold["owner_kind"] == "user"

    cleared = _mutate(
        db,
        plan.id,
        "resolve_blocker",
        {
            "node_id": node.id,
            "hold_id": block_hold["id"],
            "expected_revision": blocked["node"]["revision"],
        },
        "clear-held",
    )
    assert cleared["admission"]["condition"] == "running"
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_attempts"
    ).fetchone()[0] == 3


# Reconciliation and liveness cases continue in test_plan_admission_reconciliation.py.

