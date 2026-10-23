"""Waiting time beside working time (W6, plans-work-never-stops-2026-09-28 §8).

Fail-first: before ``node_waiting`` a node had no waiting figure at all, so
every assertion on ``["waiting"]`` here raised. Times are fixed so each
interval is exact; the facts are the rows the plan writers leave behind.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import uuid

import pytest

from infra.schema import SCHEMA_SQL
from plans import decisions, mutations, node_cost, public, scheduled_wait, service
from plans.boundary import PlanActor
from tests.plan_writer_authorizer import raw_plan_seed

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
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET state=? WHERE id=?", (new, node_id))


def _watch(db, node, armed, *, resolved=None, armed_payload=None):
    watch_id = uuid.uuid4().hex
    db.execute(
        """INSERT INTO expected_event_watches
           (id,plan_id,plan_node_id,condition_type,condition_value,status,armed_at,resolved_at,
            created_at,updated_at)
           VALUES (?,?,?,'deadline','x',?,?,?,?,?)""",
        (
            watch_id, node.plan_id, node.id,
            "armed" if resolved is None else "resolved", _at(armed),
            None if resolved is None else _at(resolved), _at(armed), _at(armed),
        ),
    )
    if armed_payload is not None:
        db.execute(
            """INSERT INTO plan_node_events
               (id,node_id,provenance,event,payload_json,created_at)
               VALUES (?,?,'test.waiting','watch_armed',?,?)""",
            (uuid.uuid4().hex, node.id, json.dumps({"watch_id": watch_id, **armed_payload}), _at(armed)),
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


def test_a_task_its_agent_set_aside_is_not_waiting_but_a_blocker_watch_is(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id)
    _move(db, node.id, "pending", "active", 0)
    _watch(db, node, 0, resolved=20, armed_payload={"blocked": {"owner_kind": "external"}})
    # The real producer, so the test reads the payload the set-aside writer leaves.
    scheduled_wait.schedule_task(
        db, node.id, reason="after the cut", deadline=_at(200), provenance="test.waiting",
    )
    db.execute(
        "UPDATE expected_event_watches SET armed_at=? WHERE plan_node_id=? AND status='armed'",
        (_at(30), node.id),
    )

    assert _waited(db, node, 90) == 20 * 60


# Waiting on the user is their time, whoever armed the wait (decision 114e0dca follow-up).


CHILD = PlanActor("child", "user-1", "project-1")


def _decision(db, plan, source, clock, raised):
    """A real decision on ``source`` in the child's plan, raised by its parent at ``raised``."""
    parent = PlanActor("owner", "user-1", "project-1")
    clock(raised)
    drafted = decisions.draft(
        plan.id, source_node_id=source.id, mutation_id=uuid.uuid4().hex, actor=CHILD,
        provenance="test.waiting", db=db, title="Which way?", context="Two ways.",
        tradeoff="Speed or reach.", recommendation="A", rationale="A is cheaper.",
        options=[{"id": "A", "label": "A", "detail": "a"}, {"id": "B", "label": "B", "detail": "b"}],
        other_enabled=True,
    ).result["decision"]
    reviewed = decisions.parent_review(
        drafted["id"], expected_revision=drafted["revision"], mutation_id=uuid.uuid4().hex,
        actor=parent, provenance="test.waiting", db=db,
    ).result["decision"]
    return decisions.raise_to_user(
        reviewed["id"], expected_revision=reviewed["revision"], mutation_id=uuid.uuid4().hex,
        actor=parent, provenance="test.waiting", db=db,
    ).result["decision"]


@pytest.fixture
def clock(monkeypatch):
    """Set the plan and registry writers' clock to ``minutes`` after T0: events are append-only."""
    from registry import models

    def set_clock(minutes):
        monkeypatch.setattr(service, "_now", lambda: _at(minutes))
        monkeypatch.setattr(models, "_now", lambda: _at(minutes))

    return set_clock


def test_a_raised_decision_is_the_users_time_until_it_is_answered(db, clock):
    plan = service.ensure_plan("thread", "child", db=db)
    node = _task(db, plan.id)
    _move(db, node.id, "pending", "active", 0)
    _watch(db, node, 0, resolved=80)
    raised = _decision(db, plan, node, clock, 20)
    clock(50)
    decisions.answer(
        raised["id"], option_id="A", expected_revision=raised["revision"],
        mutation_id=uuid.uuid4().hex, actor=PlanActor(None, "user-1", None, kind="user"),
        provenance="test.waiting", db=db,
    )

    # The agent's own watch ran 0..80; the user owed the answer 20..50.
    assert _waited(db, node, 90) == 20 * 60 + 30 * 60


def test_an_unanswered_decision_cuts_to_now_and_an_edit_back_to_draft_ends_it(db, clock):
    plan = service.ensure_plan("thread", "child", db=db)
    open_question = _task(db, plan.id, "Open")
    edited = _task(db, plan.id, "Edited")
    for node in (open_question, edited):
        _move(db, node.id, "pending", "active", 0)
        _watch(db, node, 0)
    _decision(db, plan, open_question, clock, 30)
    raised = _decision(db, plan, edited, clock, 30)
    clock(60)
    decisions.update(
        raised["id"], expected_revision=raised["revision"], mutation_id=uuid.uuid4().hex,
        actor=CHILD, provenance="test.waiting", db=db, title="Which way, again?",
    )

    assert _waited(db, open_question, 90) == 30 * 60
    assert _waited(db, edited, 90) == 30 * 60 + 30 * 60


def test_a_withdrawn_decision_or_one_dismissed_out_of_band_stops_being_the_users_time(db, clock):
    from registry import attention

    plan = service.ensure_plan("thread", "child", db=db)
    withdrawn = _task(db, plan.id, "Withdrawn")
    dismissed = _task(db, plan.id, "Dismissed")
    for node in (withdrawn, dismissed):
        _move(db, node.id, "pending", "active", 0)
        _watch(db, node, 0)
    raised = _decision(db, plan, withdrawn, clock, 20)
    shown = _decision(db, plan, dismissed, clock, 20)
    clock(50)
    decisions.invalidate(
        raised["id"], expected_revision=raised["revision"], mutation_id=uuid.uuid4().hex,
        actor=CHILD, provenance="test.waiting", db=db,
    )
    clock(60)
    # The person dismissed it from their queue; the review step stays in review.
    attention.clear_attention(shown["artifact_id"], outcome="dismiss", reason="not now", db=db)

    assert _waited(db, withdrawn, 90) == 20 * 60 + 40 * 60
    assert _waited(db, dismissed, 90) == 20 * 60 + 30 * 60


def test_a_dependency_on_the_users_step_is_their_time_but_an_agents_step_waits(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    person_step = _task(db, plan.id, "Sign in", assignee="user")
    agent_step = _task(db, plan.id, "Build")
    on_person = _task(db, plan.id, "After sign-in")
    on_agent = _task(db, plan.id, "After build")
    for node in (on_person, on_agent):
        _move(db, node.id, "pending", "active", 0)
        _watch(db, node, 0, resolved=10)
    _depend(db, on_person, person_step, 5)
    _depend(db, on_agent, agent_step, 5)
    _move(db, person_step.id, "pending", "accepted", 40)
    _move(db, agent_step.id, "pending", "accepted", 40)

    # A watch 0..10 overlaps the dependency 5..40: the user's 5..40 is cut whole.
    assert _waited(db, on_person, 90) == 5 * 60
    assert _waited(db, on_agent, 90) == 40 * 60


def test_a_user_step_closed_unaccepted_leaves_an_unmet_dependency_that_waits_again(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    person_step = _task(db, plan.id, "Sign in", assignee="user")
    on_person = _task(db, plan.id, "After sign-in")
    _move(db, on_person.id, "pending", "active", 0)
    _depend(db, on_person, person_step, 0)
    _move(db, person_step.id, "pending", "closed", 30)

    # The user's 0..30 is cut; from their Won't do on, the agent must re-plan.
    assert _waited(db, on_person, 90) == 60 * 60


def test_open_waits_leave_out_a_decision_the_user_owes(db, clock):
    plan = service.ensure_plan("thread", "child", db=db)
    node = _task(db, plan.id)
    _watch(db, node, 0, resolved=60)
    _decision(db, plan, node, clock, 20)

    assert _minutes(_open(db, ["child"], 0, 90)["child"]) == [(0, 20)]


# Open time (decision 114e0dca): each thread's outside-wait intervals for a run.


def _open(db, threads, since, until):
    return public.open_wait_intervals(
        threads, since=T0 + timedelta(minutes=since), until=T0 + timedelta(minutes=until), db=db,
    )


def _minutes(intervals):
    return [((start - T0.timestamp()) / 60, (end - T0.timestamp()) / 60) for start, end in intervals]


def test_open_waits_clip_to_the_window_and_an_open_one_ends_at_until(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id)
    _move(db, node.id, "pending", "active", 0)
    _watch(db, node, 5, resolved=15)
    _watch(db, node, 30)

    assert _minutes(_open(db, ["owner"], 10, 60)["owner"]) == [(10, 15), (30, 60)]


def test_open_waits_union_across_the_threads_own_nodes(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    prerequisite = _task(db, plan.id, "First")
    second = _task(db, plan.id, "Second")
    third = _task(db, plan.id, "Third")
    _watch(db, second, 0, resolved=30)
    _depend(db, third, prerequisite, 20)
    _move(db, prerequisite.id, "pending", "accepted", 50)

    assert _minutes(_open(db, ["owner"], 0, 90)["owner"]) == [(0, 50)]


def test_open_waits_cut_the_users_pause_and_drop_a_set_aside_task(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id)
    aside = _task(db, plan.id, "Aside")
    _watch(db, node, 0, resolved=60)
    _watch(db, aside, 0, armed_payload={"scheduled": {"reason": "later"}})
    _plan_move(db, plan.id, "active", "paused", 20)
    _plan_move(db, plan.id, "paused", "active", 40)

    assert _minutes(_open(db, ["owner"], 0, 90)["owner"]) == [(0, 20), (40, 60)]


def test_open_waits_answer_each_thread_alone_and_none_reads_empty(db):
    owner_plan = service.ensure_plan("thread", "owner", db=db)
    other_plan = service.ensure_plan("thread", "owner2", db=db)
    child_plan = service.ensure_plan("thread", "child", db=db)
    _watch(db, _task(db, owner_plan.id), 0, resolved=10)
    _watch(db, _task(db, other_plan.id), 20, resolved=30)
    _watch(db, _task(db, child_plan.id), 40, resolved=50)

    found = _open(db, ["owner", "owner2", "nobody"], 0, 90)

    assert {thread: _minutes(spans) for thread, spans in found.items()} == {
        "owner": [(0, 10)], "owner2": [(20, 30)], "nobody": [],
    }
    assert _open(db, [], 0, 90) == {}


def test_open_waits_read_a_fixed_number_of_queries_whatever_the_node_count(db):
    _waiting_plan(db, "owner", 3)
    _waiting_plan(db, "owner2", 150)

    small_count = _select_statements(db, lambda: _open(db, ["owner"], 0, 200))
    large_count = _select_statements(db, lambda: _open(db, ["owner2"], 0, 200))

    assert large_count == small_count
