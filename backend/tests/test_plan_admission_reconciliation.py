"""Reconciliation and truthful liveness invariants."""

from __future__ import annotations

import sqlite3

import pytest

from infra.schema import SCHEMA_SQL
from plans import service
from plans.boundary import PlanActor, mutate


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


def _actor():
    return PlanActor("owner", "user", None)


#: Only the user resumes, and an agent pauses only to relay them (#1969).
HUMAN = PlanActor(None, "user", None, kind="user")


def _mutate(db, plan_id, operation, params, key, *, actor=None):
    params = dict(params)
    if operation in {"add", "insert", "split", "add_follow_up", "accept_with_follow_up"}:
        params.setdefault("size", "unknown")
    return mutate(
        plan_id,
        operation,
        params,
        actor=actor or _actor(),
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


def test_acceptance_criteria_change_reselects_the_task_at_the_new_revision(db):
    """A requirements edit closes the stale attempt and reselects in one mutation.

    The edit releases with 'requirements_changed' and reselects the task, so it
    is running again at the new requirements revision with no `start`. The
    queued task behind it stays unselected (no preemption).
    """
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Revisioned")
    queued = _task(db, plan.id, "Queued behind")
    activated = _mutate(
        db,
        plan.id,
        "activate",
        {"node_id": node.id, "expected_revision": node.revision},
        "activate-revisioned",
    )
    stale_attempt = activated["admission"]["selection"]["attempt_id"]
    service.transition_node(queued.id, "active", db=db)

    edited = _mutate(
        db,
        plan.id,
        "edit",
        {
            "node_id": node.id,
            "acceptance_criteria": "The revised requirement is verified",
            "expected_revision": activated["node"]["revision"],
        },
        "edit-requirements",
    )

    assert db.in_transaction is False
    assert edited["node"]["requirements_revision"] == 2
    assert edited["admission"]["condition"] == "running"
    selection = edited["admission"]["selection"]
    assert selection["requirements_revision"] == 2
    assert selection["attempt_id"] != stale_attempt
    assert db.execute(
        "SELECT release_reason FROM plan_node_attempts WHERE id=?", (stale_attempt,),
    ).fetchone()["release_reason"] == "requirements_changed"
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL",
        (queued.id,),
    ).fetchone()[0] == 0


def test_whole_plan_pause_releases_and_resume_reacquires(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Plan pause")
    _mutate(
        db, plan.id, "activate",
        {"node_id": node.id, "expected_revision": node.revision},
        "activate-plan-pause",
    )

    paused = _mutate(
        db, plan.id, "pause",
        {"expected_revision": service._plan_row(db, plan.id)["revision"]},
        "pause-plan", actor=HUMAN,
    )
    assert paused["plan"]["state"] == "paused"
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_attempts WHERE released_at IS NULL"
    ).fetchone()[0] == 0

    resumed = _mutate(
        db, plan.id, "resume",
        {"expected_revision": paused["plan"]["revision"]},
        "resume-plan", actor=HUMAN,
    )
    assert resumed["plan"]["state"] == "active"
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_attempts WHERE released_at IS NULL"
    ).fetchone()[0] == 1


def test_watch_and_dependency_clearance_reuse_admission(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Waiter")
    prerequisite = _task(db, plan.id, "Prerequisite")
    active = _mutate(
        db, plan.id, "activate",
        {"node_id": node.id, "expected_revision": node.revision},
        "activate-waiter",
    )["node"]

    waiting = _mutate(
        db, plan.id, "arm_watch",
        {
            "node_id": node.id,
            "condition": {"type": "deadline", "value": "2099-01-01T00:00:00+00:00"},
            "expected_event": "proof.ready",
            "expected_revision": active["revision"],
        },
        "arm-waiter",
    )
    assert waiting["admission"]["condition"] == "waiting"
    resolved = _mutate(
        db, plan.id, "resolve_watch",
        {
            "node_id": node.id,
            "watch_id": waiting["watch"]["id"],
            "expected_revision": waiting["node"]["revision"],
        },
        "resolve-waiter",
    )
    assert resolved["admission"]["condition"] == "running"

    blocked = _mutate(
        db, plan.id, "add_dependency",
        {
            "node_id": node.id,
            "depends_on_node_id": prerequisite.id,
            "expected_revision": resolved["node"]["revision"],
        },
        "block-waiter",
    )
    assert blocked["admission"]["condition"] == "blocked"
    cleared = _mutate(
        db, plan.id, "remove_dependency",
        {
            "node_id": node.id,
            "depends_on_node_id": prerequisite.id,
            "expected_revision": blocked["node"]["revision"],
        },
        "clear-waiter",
    )
    assert cleared["admission"]["condition"] == "running"


def test_liveness_wakes_ready_work_while_watchdog_needs_a_selected_attempt(db):
    """Liveness and the watchdog answer different questions.

    Liveness asks "should this thread get another Turn?" — ready work counts,
    because an unattended commitment that nothing wakes never continues.
    The watchdog asks "has running work overrun its budget?" — only a genuine
    selection can overrun anything, so it still requires an open attempt.
    Conflating the two is what let a plan sit ready, unwoken and unreported.
    """
    from automation.continuation.sources.watchdog import WatchdogSource
    from plans import projections

    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Truthful wake")
    active = service.transition_node(node.id, "active", db=db)

    # Eligible, unselected, nothing holding it: actionable but not running.
    assert projections.liveness_signal("owner", db=db).node_ids == (node.id,)
    assert WatchdogSource().due("2099-01-01T00:00:00+00:00", db=db) == []

    selected = _mutate(
        db, plan.id, "start",
        {"node_id": node.id, "expected_revision": active.revision},
        "start-truthful-wake",
    )
    assert projections.liveness_signal("owner", db=db).node_ids == (node.id,)
    assert [request.subject_id for request in WatchdogSource().due(
        "2099-01-01T00:00:00+00:00", db=db,
    )] == [node.id]

    paused = _mutate(
        db, plan.id, "pause_node",
        {
            "node_id": node.id,
            "reason": "Wait for operator",
            "expected_revision": selected["node"]["revision"],
        },
        "pause-truthful-wake", actor=HUMAN,
    )
    # A held node is neither running nor ready, so both signals go quiet.
    assert paused["admission"]["condition"] == "paused"
    assert projections.liveness_signal("owner", db=db).count == 0
    assert WatchdogSource().due("2099-01-01T00:00:00+00:00", db=db) == []


def test_failed_node_follow_up_is_immediately_admissible_for_the_plan_owner(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Doomed")
    active = _mutate(
        db, plan.id, "activate",
        {"node_id": node.id, "expected_revision": node.revision},
        "activate-doomed",
    )
    attempt_id = active["admission"]["selection"]["attempt_id"]

    failed = _mutate(
        db, plan.id, "fail",
        {"node_id": node.id, "expected_revision": active["node"]["revision"]},
        "fail-doomed",
    )
    assert failed["node"]["state"] == "failed"
    assert db.execute(
        "SELECT released_at FROM plan_node_attempts WHERE id=?", (attempt_id,),
    ).fetchone()["released_at"] is not None

    follow_up = _mutate(
        db, plan.id, "add_follow_up",
        {
            "node_id": node.id,
            "title": "Recover the failed work",
            "acceptance_criteria": "The recovery is verified",
            "expected_revision": failed["node"]["revision"],
        },
        "follow-up-doomed",
    )["follow_up"]

    assert follow_up["follow_up_of"] == node.id
    assert follow_up["commitment_id"] != node.commitment_id

    # The plan owner runs the follow-up, so it is never orphaned.
    started = _mutate(
        db, plan.id, "activate",
        {"node_id": follow_up["id"], "expected_revision": follow_up["revision"]},
        "activate-follow-up",
    )
    assert started["admission"]["condition"] == "running"
    assert started["admission"]["selection"]["executor_thread_id"] == "owner"
    assert not [
        cause for cause in started["admission"]["causes"]
        if cause["reason_key"] == "assignment_missing"
    ]
