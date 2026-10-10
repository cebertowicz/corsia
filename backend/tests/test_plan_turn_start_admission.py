"""A running Turn always has a current task when its plan can give it one.

Automatic selection used to run only after a settlement mutation reconciled
inside the same write. A Turn that started with ready work already sitting in
its own plan, or a release that left the executor's slot empty with no
mutation to reconcile it, then read idle: ``current_task`` stayed null while
``next_step`` was ready. Turn admission
(``turn_admission.snapshot_after_admission``, called by ``start_idle_turn``)
reconciles the executor's own plan (``plans.admission.reconcile_plan``) before
the Turn starts: the same reconciliation every settlement and hold runs
through, not a second selection path. A started call then records under that
selection; it never selects work itself (#2168).
"""

from __future__ import annotations

import json

from infra.database import get_db
from plans import admission, lifecycle, service, tool_dispatches, turn_admission
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import add_task, dispatch_rows, open_turn, selected_plan

TOOL = "bash"


def _latest_plan_status(db, plan_id: str) -> dict:
    """Return the newest journaled ``plan.status`` projection for one plan."""
    row = db.execute(
        """SELECT summary FROM context_change_journal
           WHERE object_kind='plan' AND object_id=? AND change_kind='plan.status'
           ORDER BY seq DESC LIMIT 1""",
        (plan_id,),
    ).fetchone()
    assert row is not None, "no plan.status change was journaled"
    return json.loads(str(row["summary"]).removeprefix("Current plan status: "))


def test_turn_starting_with_one_ready_task_selects_it_before_the_first_tool_call(
    isolated_db,
):
    """Turn admission selects an active, unselected ready task before the first call."""
    thread_id = mk_session("turn-start-one-ready")
    db = get_db()
    plan = service.ensure_plan("thread", thread_id, db=db)
    task = add_task(db, plan.id, "Ready work")
    service.transition_node(task.id, "active", db=db)
    db.commit()
    turn_admission.snapshot_after_admission(thread_id, db=db)
    identity = open_turn(db, thread_id)

    recorded = tool_dispatches.record(
        thread_id, identity.turn_id, TOOL, dispatch_id="dispatch-one-ready",
    )

    assert recorded is True
    [dispatch] = dispatch_rows(db)
    open_attempt = db.execute(
        "SELECT id,node_id FROM plan_node_attempts WHERE released_at IS NULL",
    ).fetchone()
    assert open_attempt["node_id"] == task.id
    assert dispatch["attempt_id"] == open_attempt["id"]

    status = _latest_plan_status(db, plan.id)
    assert status["current_task"]["id"] == task.id
    assert status["no_current_task"] is False
    db.close()


def test_release_that_leaves_the_slot_empty_selects_the_next_ready_task_in_the_same_write(
    isolated_db,
):
    """A finished task's release with no reconcile still yields the next ready task.

    Every real settlement path reconciles in the same write it releases, so
    this reproduces the gap directly: the first task is done and its attempt
    is released with nothing to fill the slot, the way a release outside any
    mutation's own reconcile step would leave it. The first task is no longer
    active, so refilling the slot must move on to the second task, not
    reselect the first.
    """
    thread_id = mk_session("turn-start-release-empty")
    db = get_db()
    plan = service.ensure_plan("thread", thread_id, db=db)
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
    assert db.execute(
        "SELECT 1 FROM plan_node_attempts WHERE released_at IS NULL",
    ).fetchone() is None
    turn_admission.snapshot_after_admission(thread_id, db=db)
    identity = open_turn(db, thread_id)

    recorded = tool_dispatches.record(
        thread_id, identity.turn_id, TOOL, dispatch_id="dispatch-release-empty",
    )

    assert recorded is True
    open_attempt = db.execute(
        "SELECT id,node_id FROM plan_node_attempts WHERE released_at IS NULL",
    ).fetchone()
    assert open_attempt["node_id"] == second.id
    [dispatch] = dispatch_rows(db)
    assert dispatch["attempt_id"] == open_attempt["id"]
    db.close()


def test_paused_plan_selects_nothing(isolated_db):
    """A user-owned plan pause never yields its ready work to selection."""
    thread_id = mk_session("turn-start-paused-plan")
    db = get_db()
    plan = service.ensure_plan("thread", thread_id, db=db)
    task = add_task(db, plan.id, "Ready but paused")
    service.transition_node(task.id, "active", db=db)
    db.commit()
    lifecycle.transition(
        plan.id, "paused", expected_revision=service._plan_row(db, plan.id)["revision"],
        actor_id=thread_id, provenance="test", db=db,
    )
    db.commit()
    turn_admission.snapshot_after_admission(thread_id, db=db)
    identity = open_turn(db, thread_id)

    recorded = tool_dispatches.record(
        thread_id, identity.turn_id, TOOL, dispatch_id="dispatch-paused",
    )

    assert recorded is False
    assert dispatch_rows(db) == []
    assert db.execute(
        "SELECT 1 FROM plan_node_attempts WHERE released_at IS NULL",
    ).fetchone() is None
    db.close()


def test_a_stale_open_attempt_on_a_now_ineligible_task_is_released(isolated_db):
    """Turn admission releases a selection its own node no longer supports.

    A review transition that bypasses release, the way a defect elsewhere in
    the codebase could leave a node, is exactly the "invalid selection" half
    of reconciliation: the executor's plan has nothing else ready, so nothing
    new is selected, but the stale attempt does not survive the next Turn's
    admission either.
    """
    thread_id = mk_session("turn-start-stale-review-attempt")
    db = get_db()
    plan, selected = selected_plan(db, thread_id)
    service.transition_node(selected["node"]["id"], "review_requested", db=db)
    db.commit()
    stale_attempt_id = selected["admission"]["selection"]["attempt_id"]
    assert db.execute(
        "SELECT 1 FROM plan_node_attempts WHERE id=? AND released_at IS NULL",
        (stale_attempt_id,),
    ).fetchone() is not None
    turn_admission.snapshot_after_admission(thread_id, db=db)
    identity = open_turn(db, thread_id)

    recorded = tool_dispatches.record(
        thread_id, identity.turn_id, TOOL, dispatch_id="dispatch-stale-review",
    )

    assert recorded is False
    assert dispatch_rows(db) == []
    assert db.execute(
        "SELECT 1 FROM plan_node_attempts WHERE released_at IS NULL",
    ).fetchone() is None
    db.close()


def test_user_assigned_tasks_are_never_selected(isolated_db):
    """The only ready work being the user's own task selects nothing for the agent."""
    thread_id = mk_session("turn-start-user-assigned")
    db = get_db()
    plan = service.ensure_plan("thread", thread_id, db=db)
    task = add_task(db, plan.id, "User must do this", assignee="user")
    service.transition_node(task.id, "active", db=db)
    db.commit()
    turn_admission.snapshot_after_admission(thread_id, db=db)
    identity = open_turn(db, thread_id)

    recorded = tool_dispatches.record(
        thread_id, identity.turn_id, TOOL, dispatch_id="dispatch-user-assigned",
    )

    assert recorded is False
    assert dispatch_rows(db) == []
    assert db.execute(
        "SELECT 1 FROM plan_node_attempts WHERE released_at IS NULL",
    ).fetchone() is None
    db.close()
