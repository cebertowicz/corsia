"""Turn admission reconciles the executor, then snapshots its valid selection.

``start_idle_turn`` calls ``turn_admission.snapshot_after_admission`` before it writes
the Turn. Ready work is selected, stale work is released, and the resulting
attempt becomes the Turn's write-once accounting snapshot. Attempts from other
plans never count.
"""

from __future__ import annotations

import sqlite3

import pytest

from infra.input_admission import accept_thread_input
from infra.schema import SCHEMA_SQL
from infra.thread_inputs import FailedInputBatch, start_idle_turn
from plans import (
    admission,
    admission_origin,
    holds,
    node_order,
    service,
    tool_dispatches,
    turn_admission,
)
from tests.plan_execution_helpers import add_task, turn_accounting
from tests.test_plan_tool_dispatch_attribution import leave_attempt_in_plan


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    for thread_id, spawned_by in (("parent", None), ("child", "parent")):
        connection.execute(
            "INSERT INTO threads "
            "(id,user_id,spawned_by,model,created_at,updated_at) "
            "VALUES (?, 'user', ?, 'test/model', 't', 't')",
            (thread_id, spawned_by),
        )
    connection.commit()
    yield connection
    connection.close()


def _own_selection(db, title: str):
    """Select one task in the child's own plan and return (plan, task, attempt id)."""
    plan = service.ensure_plan("thread", "child", db=db)
    task = add_task(db, plan.id, title)
    activated = admission.activate(
        task.id, expected_revision=task.revision, actor_id="child",
        provenance="test.turn_snapshot", db=db,
    )
    db.commit()
    return plan, task, activated["admission"]["selection"]["attempt_id"]


def _leave_child_attempt_in_parent_plan(db) -> None:
    """Leave the child an older open attempt in its parent's plan."""
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    leave_attempt_in_plan(db, parent_plan.id, "child")


def _start_child_turn(db) -> tuple:
    accept_thread_input(
        "child", role="user", content="continue", intent="apply_now",
        message_id="child-turn-input", db=db,
    )
    batch = start_idle_turn(
        "child", turn_id="child-turn", boot_epoch="test", started_at="t", db=db,
    )
    assert batch is not None
    return turn_accounting(db, "child-turn")


def _work_turn_start_reads(db, thread_id: str, task_count: int) -> int:
    """Start an unbound Turn and count its SELECT statements."""
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?, 'user', 'test/model', 't', 't')",
        (thread_id,),
    )
    plan = service.ensure_plan("thread", thread_id, db=db)
    for index in range(task_count):
        task = add_task(db, plan.id, f"Work {index}")
        service.transition_node(task.id, "active", db=db)
    db.commit()
    accept_thread_input(
        thread_id,
        role="user",
        content="continue",
        intent="apply_now",
        message_id=f"{thread_id}-input",
        db=db,
    )
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        batch = start_idle_turn(
            thread_id,
            turn_id=f"{thread_id}-turn",
            boot_epoch="test",
            started_at="t",
            db=db,
        )
    finally:
        db.set_trace_callback(None)
    assert batch is not None
    return sum(text.lstrip().upper().startswith("SELECT") for text in statements)


def test_snapshot_failure_rolls_back_reconciliation_before_input_failure(db, monkeypatch):
    """A failed snapshot cannot leave selected work behind without a Turn."""
    plan = service.ensure_plan("thread", "child", db=db)
    task = add_task(db, plan.id, "Ready work")
    service.transition_node(task.id, "active", db=db)
    accept_thread_input(
        "child", role="user", content="continue", intent="apply_now",
        message_id="snapshot-failure-input", db=db,
    )

    def fail_snapshot(_executor, *, db):
        raise service.PlanError("injected snapshot failure")

    monkeypatch.setattr(admission_origin, "turn_snapshot", fail_snapshot)
    outcome = start_idle_turn(
        "child", turn_id="snapshot-failure-turn", boot_epoch="test",
        started_at="t", db=db,
    )

    assert isinstance(outcome, FailedInputBatch)
    assert db.execute(
        "SELECT 1 FROM plan_node_attempts WHERE plan_id=? AND released_at IS NULL",
        (plan.id,),
    ).fetchone() is None
    assert db.execute(
        "SELECT 1 FROM active_turns WHERE id='snapshot-failure-turn'",
    ).fetchone() is None


def test_turn_start_snapshots_the_executors_own_plan_selection(db):
    """The own-plan selection is snapshotted; an older attempt elsewhere never is."""
    _leave_child_attempt_in_parent_plan(db)
    own_plan, own, own_attempt = _own_selection(db, "Child-owned")

    assert _start_child_turn(db) == (
        own_plan.id, own.id, own_attempt, own.commitment_id, "execution",
    )


def test_turn_start_is_unbound_without_a_valid_own_plan_selection(db):
    """Valid first, own plan only: nothing stands in for an invalid own attempt."""
    _leave_child_attempt_in_parent_plan(db)
    _own_plan, own, own_attempt = _own_selection(db, "Child-owned")
    # An already-started call never defers release (#2705): the pause frees
    # the own attempt at once, and nothing else may stand in for it.
    db.execute(
        """INSERT INTO plan_tool_dispatches (id,attempt_id,turn_id,tool_name,started_at)
           VALUES ('open-call',?,'earlier-turn','probe','t')""",
        (own_attempt,),
    )
    holds.pause(
        own.id, "Operator paused work",
        expected_revision=service._node_row(db, own.id)["revision"],
        actor_id="child", provenance="test.turn_snapshot", db=db,
    )
    db.commit()
    assert db.execute(
        "SELECT released_at FROM plan_node_attempts WHERE id=?", (own_attempt,),
    ).fetchone()["released_at"] is not None

    assert _start_child_turn(db) == (None, None, None, None, None)
    open_attempts = """SELECT id FROM plan_node_attempts
                       WHERE released_at IS NULL AND executor_thread_id='child'"""
    before = [row["id"] for row in db.execute(open_attempts)]
    for index in range(3):
        assert (
            tool_dispatches.record(
                "child",
                "child-turn",
                "bash",
                dispatch_id=f"deferred-{index}",
            )
            is False
        )
    # Only the parent-plan attempt stays open; no call selects new work.
    assert [row["id"] for row in db.execute(open_attempts)] == before
    assert len(before) == 1 and own_attempt not in before


def test_turn_start_selects_ready_work_never_explicitly_activated(db):
    """Turn admission itself fills an empty slot; it does not wait for a first tool call.

    A task can reach ``active`` without ever going through
    ``admission.activate``/``select`` (a direct lifecycle transition, a UI
    action, a delegation write). Before Gate 1, nothing reconciled that ready
    work into a selection until the executor's first started tool call
    (``plans.tool_dispatches.record``); a Turn with no tools, or one that
    reads its own plan before ever calling a tool, saw an empty slot. Turn
    admission is the one owner that must already know.
    """
    plan = service.ensure_plan("thread", "child", db=db)
    task = add_task(db, plan.id, "Ready but never selected")
    service.transition_node(task.id, "active", db=db)
    db.commit()
    assert (
        db.execute(
            "SELECT 1 FROM plan_node_attempts WHERE node_id=?",
            (task.id,),
        ).fetchone()
        is None
    )

    plan_id, node_id, attempt_id, commitment_id, usage_role = _start_child_turn(db)

    assert (plan_id, node_id) == (plan.id, task.id)
    assert attempt_id is not None
    assert commitment_id == task.commitment_id
    assert usage_role == "execution"
    open_attempt = db.execute(
        "SELECT node_id FROM plan_node_attempts WHERE released_at IS NULL",
    ).fetchone()
    assert open_attempt["node_id"] == task.id


def test_turn_start_fills_a_slot_a_release_left_empty(db):
    """A release with no reconcile of its own is still filled at Turn admission.

    A defective release elsewhere in the codebase (one that skips
    ``reselect_and_reconcile``) can leave a plan with ready work and an empty
    slot before any Turn exists. This reproduces that shape directly: the
    first task's attempt is released with nothing to fill the slot, the way
    such a release would leave it. Turn admission must move on to the second
    task, not reselect the first.
    """
    plan = service.ensure_plan("thread", "child", db=db)
    first = add_task(db, plan.id, "First ready", order_key=0)
    second = add_task(db, plan.id, "Second ready", order_key=1)
    service.transition_node(first.id, "active", db=db)
    service.transition_node(second.id, "active", db=db)
    admission.reconcile_plan(plan.id, actor_id=None, provenance="test", db=db)
    db.commit()
    held = db.execute(
        "SELECT node_id FROM plan_node_attempts WHERE released_at IS NULL",
    ).fetchone()
    assert held["node_id"] == first.id
    service.transition_node(first.id, "failed", db=db)
    assert admission.release(first.id, "test_setup_empty_slot", db=db)
    db.commit()
    assert (
        db.execute(
            "SELECT 1 FROM plan_node_attempts WHERE released_at IS NULL",
        ).fetchone()
        is None
    )

    plan_id, node_id, attempt_id, _commitment_id, usage_role = _start_child_turn(db)

    assert (plan_id, node_id) == (plan.id, second.id)
    assert attempt_id is not None
    assert usage_role == "execution"


def test_turn_start_selects_nothing_for_a_paused_plan(db):
    """A user-owned plan pause never yields its ready work to Turn admission."""
    plan = service.ensure_plan("thread", "child", db=db)
    task = add_task(db, plan.id, "Ready but paused plan")
    service.transition_node(task.id, "active", db=db)
    db.commit()
    from plans import lifecycle

    lifecycle.transition(
        plan.id,
        "paused",
        expected_revision=service._plan_row(db, plan.id)["revision"],
        actor_id="child",
        provenance="test",
        db=db,
    )
    db.commit()

    assert _start_child_turn(db) == (None, None, None, None, None)
    assert (
        db.execute(
            "SELECT 1 FROM plan_node_attempts WHERE released_at IS NULL",
        ).fetchone()
        is None
    )


def test_turn_snapshot_uses_the_canonical_reconciliation_decision(db, monkeypatch):
    """No eligible task means Turn admission skips locked reconciliation."""
    plan = service.ensure_plan("thread", "child", db=db)
    calls: list[str] = []

    def decide(connection, plan_id):
        assert connection is db
        calls.append(plan_id)
        return turn_admission._ReconciliationDecision(False)

    monkeypatch.setattr(
        turn_admission, "_owner_reconciliation_decision", decide,
    )
    monkeypatch.setattr(
        admission,
        "reconcile_plan",
        lambda *_args, **_kwargs: pytest.fail("reconciliation predicate diverged"),
    )

    assert turn_admission.snapshot_after_admission("child", db=db) is None
    assert calls == [plan.id]


def test_turn_start_never_selects_user_assigned_work(db):
    """A human obligation never becomes the agent Turn's work or accounting."""
    plan = service.ensure_plan("thread", "child", db=db)
    task = add_task(db, plan.id, "User must do this", assignee="user")
    service.transition_node(task.id, "active", db=db)
    db.commit()

    assert _start_child_turn(db) == (None, None, None, None, None)
    assert (
        db.execute(
            "SELECT 1 FROM plan_node_attempts WHERE node_id=?",
            (task.id,),
        ).fetchone()
        is None
    )


def test_turn_start_skips_held_work_and_selects_the_first_eligible_task(db):
    """Canonical order is preserved after batched eligibility excludes a held task."""
    plan = service.ensure_plan("thread", "child", db=db)
    prerequisite = add_task(db, plan.id, "Prerequisite", order_key=0)
    held = add_task(db, plan.id, "Held first", order_key=1)
    first_ready = add_task(db, plan.id, "First eligible", order_key=2)
    second_ready = add_task(db, plan.id, "Second eligible", order_key=3)
    service.add_dependency(held.id, prerequisite.id, db=db)
    for task in (held, first_ready, second_ready):
        service.transition_node(task.id, "active", db=db)
    db.commit()

    plan_id, node_id, attempt_id, _commitment_id, usage_role = _start_child_turn(db)

    assert (plan_id, node_id) == (plan.id, first_ready.id)
    assert attempt_id is not None
    assert usage_role == "execution"


def test_turn_start_releases_a_stale_ineligible_attempt(db):
    """Admission clears a stale attempt before snapshotting an unbound Turn."""
    _plan, task, attempt_id = _own_selection(db, "Stale work")
    service.transition_node(task.id, "review_requested", db=db)
    db.commit()
    assert (
        db.execute(
            "SELECT released_at FROM plan_node_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()["released_at"]
        is None
    )

    assert _start_child_turn(db) == (None, None, None, None, None)
    assert (
        db.execute(
            "SELECT released_at FROM plan_node_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()["released_at"]
        is not None
    )


def test_turn_start_scans_past_64_ineligible_tasks_with_one_bounded_cursor(
    db, monkeypatch,
):
    """Turn admission crosses two full eligibility batches through one SQL cursor."""
    thread_id = "many-user-tasks"
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?, 'user', 'test/model', 't', 't')",
        (thread_id,),
    )
    plan = service.ensure_plan("thread", thread_id, db=db)
    for index in range(node_order.ACTIVE_TASK_PAGE_SIZE * 2 + 1):
        task = add_task(db, plan.id, f"User work {index}", assignee="user")
        service.transition_node(task.id, "active", db=db)
    eligible = add_task(db, plan.id, "First eligible work")
    service.transition_node(eligible.id, "active", db=db)
    db.commit()
    accept_thread_input(
        thread_id,
        role="user",
        content="continue",
        intent="apply_now",
        message_id=f"{thread_id}-input",
        db=db,
    )
    ordered_calls: list[tuple[str, bool]] = []
    fetch_sizes: list[int] = []
    result_sizes: list[int] = []
    real_ordered_cursor = node_order.ordered_cursor

    class RecordingCursor:
        def __init__(self, cursor):
            self._cursor = cursor

        def fetchmany(self, size):
            fetch_sizes.append(size)
            rows = self._cursor.fetchmany(size)
            result_sizes.append(len(rows))
            return rows

    def recording_cursor(connection, plan_id, *, active_tasks_only=False):
        cursor = real_ordered_cursor(
            connection,
            plan_id,
            active_tasks_only=active_tasks_only,
        )
        if not active_tasks_only:
            return cursor
        ordered_calls.append((plan_id, active_tasks_only))
        return RecordingCursor(cursor)

    monkeypatch.setattr(node_order, "ordered_cursor", recording_cursor)

    batch = start_idle_turn(
        thread_id,
        turn_id=f"{thread_id}-turn",
        boot_epoch="test",
        started_at="t",
        db=db,
    )

    assert batch is not None
    assert turn_accounting(db, f"{thread_id}-turn")[1] == eligible.id
    assert ordered_calls == [(plan.id, True)]
    assert fetch_sizes == [node_order.ACTIVE_TASK_PAGE_SIZE] * 3
    assert result_sizes == [node_order.ACTIVE_TASK_PAGE_SIZE] * 2 + [2]


def test_turn_start_read_count_is_flat_for_eligible_work(db):
    """Selecting the first of 100 ready tasks takes a fixed query bundle."""
    one = _work_turn_start_reads(db, "one-ready-task", 1)
    hundred = _work_turn_start_reads(db, "hundred-ready-tasks", 100)

    assert hundred == one <= 120
