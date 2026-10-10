"""Recording a started call costs the same however many plans hold its executor's attempts.

Every started tool call runs ``tool_dispatches.record``: one validity check
before taking the write lock and one inside it. Only the executor's own thread
plan serves its calls, so open attempts older data left it in other threads'
plans are never read as candidates: neither the whole call nor its locked
section reads more when more plans hold one.

The same call also runs for a thread with no valid selection at all, on every
tool call of its Turn, so a plan with nothing selected -- everything pending,
an active task that is entirely the user's own, or ready work Turn admission
has not selected -- must clear at the same fixed, small read cost whatever its
size, and must never take the write lock. A call never selects work (#2168).
"""

from __future__ import annotations

from infra.database import get_db
from plans import admission, service, tool_dispatches
from tests.plan_execution_helpers import add_task, dispatch_rows
from tests.test_plan_tool_dispatch_attribution import leave_attempt_in_plan

WORKER = "worker"
TURN = "turn-query-cost"


def _thread(db, thread_id: str) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?, 'user', 'test/model', 't', 't')",
        (thread_id,),
    )


def _running_worker(db) -> None:
    """Insert the worker thread and its running Turn, with no selection of its own."""
    _thread(db, WORKER)
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES (?,?,0,'test/model','running','t','test')""",
        (TURN, WORKER),
    )
    db.commit()


def _seed_selected_worker(db) -> None:
    """Insert the worker thread, its running Turn, and a selection in its own plan."""
    _running_worker(db)
    plan = service.ensure_plan("thread", WORKER, db=db)
    task = service.add_node(
        plan.id, kind="task", title="Own task",
        acceptance_criteria="The task is verified", db=db,
    )
    admission.activate(
        task.id, expected_revision=task.revision, actor_id=WORKER,
        provenance="test.query_cost", db=db,
    )
    db.commit()


def _leave_worker_attempt_in_new_plan(db, index: int) -> None:
    """Leave the worker an open attempt in a new plan another thread owns."""
    owner = f"owner-{index}"
    _thread(db, owner)
    plan = service.ensure_plan("thread", owner, db=db)
    leave_attempt_in_plan(db, plan.id, WORKER)


def _record_reads(db, dispatch_id: str, *, expected: bool = True) -> tuple[int, int]:
    """Record one call and return (SELECTs, SELECTs inside the write lock)."""
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        assert tool_dispatches.record(WORKER, TURN, "bash", dispatch_id=dispatch_id) is expected
    finally:
        db.set_trace_callback(None)
    selects = locked = 0
    in_lock = False
    for statement in statements:
        text = statement.lstrip().upper()
        if text.startswith("BEGIN IMMEDIATE"):
            in_lock = True
        elif text.startswith(("COMMIT", "ROLLBACK")):
            in_lock = False
        elif text.startswith("SELECT"):
            selects += 1
            locked += int(in_lock)
    return selects, locked


def _all_pending_worker(db, count: int) -> None:
    """A running worker whose own plan holds only pending, never-active tasks."""
    _running_worker(db)
    plan = service.ensure_plan("thread", WORKER, db=db)
    for index in range(count):
        add_task(db, plan.id, f"Pending {index}")
    db.commit()


def _lone_user_assigned_active_worker(db) -> None:
    """A running worker whose own plan's only active task belongs to the user."""
    _running_worker(db)
    plan = service.ensure_plan("thread", WORKER, db=db)
    task = add_task(db, plan.id, "User must do this", assignee="user")
    service.transition_node(task.id, "active", db=db)
    db.commit()


def test_a_plan_with_only_pending_tasks_never_takes_the_lock_or_grows(isolated_db):
    """20 pending tasks cost the same fixed reads as one, and never lock."""
    db = get_db()
    try:
        _all_pending_worker(db, count=20)

        first = _record_reads(db, "call-all-pending-1", expected=False)
        second = _record_reads(db, "call-all-pending-2", expected=False)
        third = _record_reads(db, "call-all-pending-3", expected=False)

        assert first[1] == 0
        assert second == first
        assert third == first
    finally:
        db.close()


def test_a_lone_user_assigned_active_task_never_takes_the_lock_or_grows(isolated_db):
    """The only ready work being the user's own never opens the write lock."""
    db = get_db()
    try:
        _lone_user_assigned_active_worker(db)

        first = _record_reads(db, "call-user-assigned-1", expected=False)
        second = _record_reads(db, "call-user-assigned-2", expected=False)
        third = _record_reads(db, "call-user-assigned-3", expected=False)

        assert first[1] == 0
        assert second == first
        assert third == first
    finally:
        db.close()


def _add_held_active_task(db, plan_id: str, prerequisite_id: str, index: int) -> None:
    """Add one active task held behind an unfinished prerequisite."""
    task = add_task(db, plan_id, f"Held {index}")
    service.add_dependency(task.id, prerequisite_id, db=db)
    service.transition_node(task.id, "active", db=db)
    db.commit()


def test_held_active_tasks_never_take_the_lock_and_cost_the_same_however_many(
    isolated_db,
):
    """Five dependency-held active tasks cost the same reads as one, and never lock."""
    db = get_db()
    try:
        _running_worker(db)
        plan = service.ensure_plan("thread", WORKER, db=db)
        prerequisite = add_task(db, plan.id, "Not started yet")
        _add_held_active_task(db, plan.id, prerequisite.id, 0)

        one_held = _record_reads(db, "call-one-held", expected=False)
        for index in range(1, 5):
            _add_held_active_task(db, plan.id, prerequisite.id, index)
        five_held = _record_reads(db, "call-five-held", expected=False)

        assert one_held[1] == 0
        assert five_held == one_held
    finally:
        db.close()


def test_unselected_ready_work_never_takes_the_lock_or_grows(isolated_db):
    """Ready work nothing selected costs the same fixed reads, never locks, stays unselected."""
    db = get_db()
    try:
        _running_worker(db)
        plan = service.ensure_plan("thread", WORKER, db=db)
        for index in range(3):
            task = add_task(db, plan.id, f"Ready work {index}")
            service.transition_node(task.id, "active", db=db)
        db.commit()

        first = _record_reads(db, "call-ready-1", expected=False)
        second = _record_reads(db, "call-ready-2", expected=False)

        assert first[1] == 0
        assert second == first
        assert dispatch_rows(db) == []
        assert db.execute(
            "SELECT 1 FROM plan_node_attempts WHERE released_at IS NULL",
        ).fetchone() is None
    finally:
        db.close()


def test_record_reads_the_same_statements_however_many_plans_hold_worker_attempts(
    isolated_db,
):
    db = get_db()
    try:
        _seed_selected_worker(db)
        own_plan_only = _record_reads(db, "call-own-plan-only")

        for index in range(3):
            _leave_worker_attempt_in_new_plan(db, index)
        with_three_more_plans = _record_reads(db, "call-three-more-plans")

        assert with_three_more_plans == own_plan_only
        assert own_plan_only[1] > 0
    finally:
        db.close()
