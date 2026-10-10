"""Waiting time beside working time (W6, plans-work-never-stops-2026-09-28 §8).

Fail-first: before ``node_waiting`` a node had no waiting figure at all, so
every assertion on ``["waiting"]`` here raised. Times are fixed so each
interval is exact; the facts are the rows the plan writers leave behind.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
import uuid

import pytest

from infra.schema import SCHEMA_SQL
from plans import mutations, node_cost, service

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def _at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project-1','Project','t','t')"
    )
    for thread_id, spawned_by in (("owner", None), ("owner2", None), ("child", "owner")):
        connection.execute(
            """INSERT INTO threads (id,user_id,project_id,spawned_by,model,created_at,updated_at)
               VALUES (?,'user-1','project-1',?,'test/model','t','t')""",
            (thread_id, spawned_by),
        )
    connection.commit()
    yield connection
    connection.close()


def _task(db, plan_id, title="Task", **kwargs):
    node = service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria=f"{title} DOD", db=db, **kwargs,
    )
    db.execute("UPDATE plan_nodes SET created_at=? WHERE id=?", (_at(0), node.id))
    return node


def _event(db, node_id, event, minutes, *, prior=None, new=None, depends_on=None):
    payload = '{"depends_on_node_id": "%s"}' % depends_on if depends_on else None
    db.execute(
        """INSERT INTO plan_node_events
           (id,node_id,provenance,event,prior_state,new_state,payload_json,created_at)
           VALUES (?,?,'test.waiting',?,?,?,?,?)""",
        (uuid.uuid4().hex, node_id, event, prior, new, payload, _at(minutes)),
    )


def _move(db, node_id, prior, new, minutes):
    _event(db, node_id, "state_transition", minutes, prior=prior, new=new)
    db.execute("UPDATE plan_nodes SET state=? WHERE id=?", (new, node_id))


def _watch(db, node, armed, *, resolved=None):
    db.execute(
        """INSERT INTO expected_event_watches
           (id,plan_id,plan_node_id,condition_type,condition_value,status,armed_at,resolved_at,
            created_at,updated_at)
           VALUES (?,?,?,'deadline','x',?,?,?,?,?)""",
        (
            uuid.uuid4().hex, node.plan_id, node.id,
            "armed" if resolved is None else "resolved", _at(armed),
            None if resolved is None else _at(resolved), _at(armed), _at(armed),
        ),
    )


def _depend(db, node, prerequisite, minutes):
    db.execute(
        "INSERT INTO plan_node_dependencies (node_id,depends_on_node_id,created_at) VALUES (?,?,?)",
        (node.id, prerequisite.id, _at(minutes)),
    )
    _event(db, node.id, "dependency_added", minutes, depends_on=prerequisite.id)


def _hold(db, node, owner_kind, start, end, kind="paused"):
    cleared = end is not None
    clearance = None
    if cleared:
        _event(db, node.id, "hold_cleared", end)
        clearance = db.execute(
            "SELECT id FROM plan_node_events WHERE node_id=? ORDER BY rowid DESC LIMIT 1",
            (node.id,),
        ).fetchone()["id"]
    db.execute(
        """INSERT INTO plan_node_holds
           (id,node_id,kind,reason_key,reason,owner_kind,provenance,created_at,
            cleared_by_actor_id,cleared_at,clearance_event_id)
           VALUES (?,?,?,'manual_pause','r',?,'test',?,?,?,?)""",
        (
            uuid.uuid4().hex, node.id, kind, owner_kind, _at(start),
            "someone" if cleared else None, _at(end) if cleared else None, clearance,
        ),
    )


def _waited(db, node, minutes, *, rolled=False):
    costs = node_cost.project_many([node.plan_id], db=db, now=T0 + timedelta(minutes=minutes))
    return costs[node.plan_id][node.id]["rolled" if rolled else "own"]["waiting"]["seconds"]


def test_a_resolved_watch_counts_its_interval_and_an_armed_one_runs_to_now(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id)
    _move(db, node.id, "pending", "active", 1)
    _watch(db, node, 10, resolved=40)
    _watch(db, node, 50)

    assert _waited(db, node, 60) == 30 * 60 + 10 * 60


def test_an_unmet_dependency_waits_until_its_prerequisite_is_accepted(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    prerequisite = _task(db, plan.id, "First")
    node = _task(db, plan.id, "Second")
    _depend(db, node, prerequisite, 5)
    _move(db, prerequisite.id, "pending", "active", 6)
    _move(db, prerequisite.id, "active", "accepted", 25)

    assert _waited(db, node, 90) == 20 * 60


def test_a_removed_dependency_stops_waiting_when_it_is_removed(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    prerequisite = _task(db, plan.id, "First")
    node = _task(db, plan.id, "Second")
    _depend(db, node, prerequisite, 0)
    db.execute("DELETE FROM plan_node_dependencies WHERE node_id=?", (node.id,))
    _event(db, node.id, "dependency_removed", 15, depends_on=prerequisite.id)

    assert _waited(db, node, 90) == 15 * 60


def test_review_waits_only_while_the_node_links_a_registered_pull_request(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    with_pr = _task(db, plan.id, "With PR")
    without_pr = _task(db, plan.id, "Without PR")
    for node in (with_pr, without_pr):
        _move(db, node.id, "pending", "active", 1)
        _move(db, node.id, "active", "review_requested", 10)
    db.execute(
        """INSERT INTO artifact_identities (id,integration,type,external_id,created_at,updated_at)
           VALUES ('pr-1','github','pull-request','o/r#1','t','t')"""
    )
    db.execute(
        """INSERT INTO plan_node_artifact_links (node_id,artifact_id,role,provenance,created_at)
           VALUES (?,'pr-1','result','test',?)""",
        (with_pr.id, _at(5)),
    )

    assert _waited(db, with_pr, 70) == 60 * 60
    assert _waited(db, without_pr, 70) == 0


def test_the_users_pause_is_not_waiting_but_an_agents_hold_is_no_cut(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    user_paused = _task(db, plan.id, "User paused")
    agent_paused = _task(db, plan.id, "Agent paused")
    for node in (user_paused, agent_paused):
        _move(db, node.id, "pending", "active", 0)
        _watch(db, node, 0, resolved=60)
    _hold(db, user_paused, "user", 20, 50)
    _hold(db, agent_paused, "thread", 20, 50)

    assert _waited(db, user_paused, 90) == 30 * 60
    assert _waited(db, agent_paused, 90) == 60 * 60


def _plan_move(db, plan_id, prior, new, minutes):
    db.execute(
        """INSERT INTO plan_events (id,plan_id,provenance,event,prior_state,new_state,created_at)
           VALUES (?,?,'test.waiting','state_transition',?,?,?)""",
        (uuid.uuid4().hex, plan_id, prior, new, _at(minutes)),
    )


def test_a_whole_plan_pause_is_the_users_time_not_waiting(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id)
    _move(db, node.id, "pending", "active", 0)
    _watch(db, node, 0)
    _plan_move(db, plan.id, "active", "paused", 20)
    _plan_move(db, plan.id, "paused", "active", 50)
    _plan_move(db, plan.id, "active", "paused", 80)

    # Watch armed 0..90; the plan was paused 20..50 and again from 80 on.
    assert _waited(db, node, 90) == 20 * 60 + 30 * 60


def test_a_user_blocker_still_open_cuts_to_now(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id)
    _watch(db, node, 0)
    _hold(db, node, "user", 30, None, kind="blocked")

    assert _waited(db, node, 90) == 30 * 60


def test_overlapping_causes_count_once(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    prerequisite = _task(db, plan.id, "First")
    node = _task(db, plan.id, "Second")
    _watch(db, node, 0, resolved=30)
    _watch(db, node, 5, resolved=20)
    _depend(db, node, prerequisite, 10)
    _move(db, prerequisite.id, "pending", "accepted", 40)

    assert _waited(db, node, 90) == 40 * 60


def test_nothing_counts_after_the_node_settles_or_before_any_wait(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id)
    _move(db, node.id, "pending", "active", 20)
    _watch(db, node, 30)
    _move(db, node.id, "active", "accepted", 45)

    assert _waited(db, node, 120) == 15 * 60


def test_waiting_rolls_up_through_an_attached_child_plan_and_never_into_cost(db):
    parent_plan = service.ensure_plan("thread", "owner", db=db)
    delegate = _task(db, parent_plan.id, "Delegate", spawn_kind="claude-code")
    service.transition_node(delegate.id, "active", db=db)
    service.ensure_plan("thread", "child", db=db)
    mutations.attach_spawned_thread(delegate.id, "child", db=db)
    child_plan = db.execute(
        "SELECT * FROM plans WHERE owner_kind='thread' AND owner_id='child'"
    ).fetchone()
    phase = service.add_node(
        child_plan["id"], kind="phase", title="Child phase", acceptance_criteria="d", db=db,
    )
    child_task = _task(db, child_plan["id"], "Child work", parent_id=phase.id)
    _watch(db, child_task, 0, resolved=10)

    costs = node_cost.project_many(
        [parent_plan.id], db=db, now=T0 + timedelta(minutes=30),
    )[parent_plan.id][delegate.id]

    assert costs["own"]["waiting"] == {"seconds": 0}
    assert costs["rolled"]["waiting"] == {"seconds": 600}
    assert costs["rolled"]["tokens"] == {"known": 0, "unknown_rows": 0}
    assert costs["rolled"]["model_cost_usd"] == {"known": 0, "unknown_rows": 0}
    assert costs["rolled"]["execution"]["seconds"] == 0


def _select_statements(db, fn) -> int:
    statements: list[str] = []
    db.set_trace_callback(
        lambda statement: (
            statements.append(statement) if statement.lstrip().upper().startswith("SELECT") else None
        )
    )
    try:
        fn()
    finally:
        db.set_trace_callback(None)
    return len(statements)


def _waiting_plan(db, owner, width):
    plan = service.ensure_plan("thread", owner, db=db)
    first = _task(db, plan.id, "First")
    for index in range(width):
        node = _task(db, plan.id, f"Task {index}")
        _watch(db, node, index, resolved=index + 5)
        _depend(db, node, first, index)
        _hold(db, node, "user", index, index + 1)
        _move(db, node.id, "pending", "active", index)
    db.commit()
    return plan


def test_waiting_adds_a_fixed_number_of_queries_whatever_the_node_count(db):
    small = _waiting_plan(db, "owner", 3)
    large = _waiting_plan(db, "owner2", 150)

    small_count = _select_statements(db, lambda: node_cost.project_many([small.id], db=db))
    large_count = _select_statements(db, lambda: node_cost.project_many([large.id], db=db))

    assert large_count == small_count
