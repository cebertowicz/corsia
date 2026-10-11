"""Every routed continuation event is rechecked before a Turn claims it.

A wake is delivered while its source describes something the thread can act
on. It then waits behind whatever Turn is running, and by the time a Turn would
claim it the parent can have settled the delegated step, the watched task can
be archived, the schedule cancelled. Starting a
Turn for it then costs a model call and produces nothing.

T0 and T1 wakes already carried their currency to the claim, and a plan wake
about a delegated step carries it too. An expired watch and a due schedule
did not: their events reached
the parent with no condition, and no claim ever asked again. Each source now
answers the claim's one question -- can the work this wake described still
move -- and, as with T0, it must not answer from bookkeeping that delivery
itself changed: an expired watch and an exhausted one-shot schedule are what a
*delivered* wake looks like, not a stale one.
"""

from __future__ import annotations

import sqlite3
import uuid

from automation import schedules, watches
from automation.continuation.execution import event_for
from automation.continuation.sources.schedules import ScheduleSource
from automation.continuation.sources.watch_expiry import WatchExpirySource
from automation.continuation.validity import condition_for_event
from infra.schema import SCHEMA_SQL
from infra.thread_inputs import start_idle_turn
from plans import archival, mutations, service
from tests.plan_review_helpers import submit_review_evidence
from tests.plan_writer_authorizer import raw_plan_seed

NOW = "2026-08-01T00:00:00+00:00"
TURN = "turn-running"


def _db(monkeypatch) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project-1','Project','t','t')"
    )
    for thread_id, spawned_by in (("parent", None), ("worker", "parent")):
        db.execute(
            """INSERT INTO threads
               (id,user_id,project_id,spawned_by,model,created_at,updated_at)
               VALUES (?,'user-1','project-1',?,'test/model','t','t')""",
            (thread_id, spawned_by),
        )
    db.commit()
    return db


def _delegated(db, title="Work", *, still_running: bool = False):
    """The parent's delegated step and the worker's own step attached to it.

    ``still_running`` adds a second active step, so a review of the first is
    intermediate -- routed -- rather than the worker's final return.
    """
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    delegated = service.add_node(
        parent_plan.id, kind="task", title="Delegate work",
        acceptance_criteria="Worker result is reviewed",
        spawn_kind="claude-code", db=db,
    )
    service.transition_node(delegated.id, "active", db=db)
    plan = service.ensure_plan("thread", "worker", db=db)
    node = service.add_node(
        plan.id, kind="task", title=title, acceptance_criteria="Tests pass", db=db,
    )
    mutations.attach_spawned_thread(delegated.id, "worker", db=db)
    if still_running:
        other = service.add_node(
            plan.id, kind="task", title="Other work", acceptance_criteria="Done", db=db,
        )
        service.transition_node(other.id, "active", db=db)
    return plan, delegated, node


def _accept(db, node_id: str, by: str) -> None:
    submit_review_evidence(node_id, db=db)
    service.transition_node(
        node_id, "accepted", verification_result="passed", verified_by=by, db=db,
    )


def _condition(request) -> dict:
    event, _summary, details = event_for(request)
    condition = condition_for_event(event, details)
    assert condition is not None, f"{event} carries no currency to the claim"
    return condition


def _queue(db, thread_id: str, condition: dict) -> str:
    """Queue the delivered wake behind a running Turn, as its delivery does."""
    from infra.thread_driver import admit_input

    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES (?,?,0,'test/model','running','t','boot')""",
        (TURN, thread_id),
    )
    message_id = uuid.uuid4().hex
    admit_input(
        thread_id, role="system", content="routed event", intent="apply_now",
        message_id=message_id, turn_id=uuid.uuid4().hex, boot_epoch="boot",
        admitted_at="t", continuation=condition, db=db,
    )
    db.commit()
    return message_id


def _claim(db, thread_id: str, message_id: str) -> tuple:
    """End the running Turn and let the thread claim; return (started, row)."""
    db.execute("UPDATE active_turns SET status='completed' WHERE id=?", (TURN,))
    db.commit()
    started = start_idle_turn(
        thread_id, turn_id="turn-next", boot_epoch="boot", started_at="t", db=db,
    )
    row = db.execute(
        "SELECT delivery_state,delivery_error FROM messages WHERE id=?",
        (message_id,),
    ).fetchone()
    return started, tuple(row)


STALE = ("cancelled", "stale_plan_wake")


# --- a child's failed step, to its parent ------------------------------------


def _failed_with_work_behind_it(db):
    """The wake a worker's Turn end raises for a failure with work behind it.

    It is a plan wake about the delegating step, so it stays current exactly
    while that step still awaits this worker (``plans.wake_currency``).
    """
    from plans import wake_currency

    plan, delegated, node = _delegated(db, still_running=True)
    service.transition_node(node.id, "active", db=db)
    mutations.fail_node(node.id, db=db)
    condition = wake_currency.condition(
        plan_id=delegated.plan_id, kind="child_step_failed", subject_id="turn-1",
        revision=0, delegated_step_id=delegated.id, delegated_worker="worker",
    )
    return delegated, _queue(db, "parent", condition)


def test_a_failure_still_standing_starts_its_turn(monkeypatch):
    db = _db(monkeypatch)
    try:
        _delegated_step, message_id = _failed_with_work_behind_it(db)

        started, row = _claim(db, "parent", message_id)
        assert started is not None and row[0] != "cancelled"
    finally:
        db.close()


def test_a_failure_whose_delegating_step_settled_does_not_start_a_turn(monkeypatch):
    """#1991: the parent already settled the step, so there is nothing to decide."""
    db = _db(monkeypatch)
    try:
        delegated_step, message_id = _failed_with_work_behind_it(db)

        mutations.fail_node(delegated_step.id, db=db)

        started, row = _claim(db, "parent", message_id)
        assert started is None and row == STALE
    finally:
        db.close()


# --- an expired watch, to its owner ------------------------------------------


def _watched(db):
    plan = service.ensure_plan("thread", "parent", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Wait for CI",
        acceptance_criteria="CI is green", db=db,
    )
    service.transition_node(node.id, "active", db=db)
    db.execute("BEGIN")
    watch = watches.arm_watch(
        node.id, {"type": "deadline", "value": "2000-01-01T00:00:00+00:00"},
        owner_thread="parent", plan_id=plan.id, plan_node_id=node.id,
        db=db,
    )
    db.commit()
    requests = WatchExpirySource().due(NOW, db=db)
    assert [r.subject_id for r in requests] == [watch.id]
    return node, watch, _condition(requests[0])


def test_an_expiry_whose_task_was_archived_does_not_start_a_turn(monkeypatch):
    db = _db(monkeypatch)
    try:
        node, watch, condition = _watched(db)
        message_id = _queue(db, "parent", condition)

        db.execute("BEGIN")
        watches.expire_watch(watch.id, db=db)
        with raw_plan_seed(db):
            db.execute(
                "UPDATE plan_nodes SET archived_at='t' WHERE id=?", (node.id,)
            )
        db.commit()

        started, row = _claim(db, "parent", message_id)
        assert started is None and row == STALE
    finally:
        db.close()


def test_a_delivered_expiry_still_starts_its_turn_while_the_task_is_open(monkeypatch):
    db = _db(monkeypatch)
    try:
        _node, watch, condition = _watched(db)
        message_id = _queue(db, "parent", condition)

        # Delivery settles the source: the watch is expired, not stale.
        db.execute("BEGIN")
        watches.expire_watch(watch.id, db=db)
        db.commit()

        started, row = _claim(db, "parent", message_id)
        assert started is not None and row[0] != "cancelled"
    finally:
        db.close()


# --- a due schedule, to its thread -------------------------------------------


def _scheduled(db, max_runs: int):
    schedule = schedules.create(
        "parent", "project-1", "parent", kind="cadence", spec="60",
        max_runs=max_runs, now="2026-01-01T00:00:00+00:00", db=db,
    )
    requests = ScheduleSource().due(NOW, db=db)
    assert [r.subject_id for r in requests] == [schedule.id]
    return schedule, _condition(requests[0])


def test_a_schedule_its_actor_cancelled_does_not_start_a_turn(monkeypatch):
    db = _db(monkeypatch)
    try:
        schedule, condition = _scheduled(db, max_runs=3)
        message_id = _queue(db, "parent", condition)

        assert schedules.cancel(
            schedule.id, thread_id="parent", actor_id="parent", db=db,
        )
        db.commit()

        started, row = _claim(db, "parent", message_id)
        assert started is None and row == STALE
    finally:
        db.close()


def test_a_delivered_one_shot_schedule_still_starts_its_turn(monkeypatch):
    db = _db(monkeypatch)
    try:
        schedule, condition = _scheduled(db, max_runs=1)
        message_id = _queue(db, "parent", condition)

        # Delivery settles the source: the last run is exhausted, not stale.
        schedules.settle_run(schedule.id, NOW, db=db)
        db.commit()

        started, row = _claim(db, "parent", message_id)
        assert started is not None and row[0] != "cancelled"
    finally:
        db.close()
