"""A parent's rolled Working and Waiting count wall-clock time once (#893).

Fail-first: ``node_cost._add`` summed children's seconds, so steps that ran or
waited in parallel made a parent read longer than the phase had existed. A
rolled figure is now the union of its subtree's intervals: Working is the
union of Turn intervals, Waiting is the union of waits minus Working.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
import uuid

import pytest

from infra.schema import SCHEMA_SQL
from plans import mutations, node_cost, node_time, service
from tests.plan_writer_authorizer import raw_plan_seed

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
MINUTE = 60


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


def _phase(db, plan_id, title="Phase"):
    return service.add_node(
        plan_id, kind="phase", title=title, acceptance_criteria=f"{title} DOD", db=db,
    )


def _watch(db, node, armed, resolved):
    db.execute(
        """INSERT INTO expected_event_watches
           (id,plan_id,plan_node_id,condition_type,condition_value,status,armed_at,resolved_at,
            created_at,updated_at)
           VALUES (?,?,?,'deadline','x','resolved',?,?,?,?)""",
        (uuid.uuid4().hex, node.plan_id, node.id, _at(armed), _at(resolved), _at(armed), _at(armed)),
    )


def _turn(db, node, start, end, thread_id="owner"):
    turn_id = uuid.uuid4().hex
    db.execute(
        """INSERT INTO active_turns (
               id,thread_id,execution_epoch,model,status,started_at,ended_at,boot_epoch,
               plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role
           ) VALUES (?,?,0,'test-model','completed',?,?,'boot',?,?,?,?,'execution')""",
        (
            turn_id, thread_id, _at(start), _at(end),
            node.plan_id, node.id, f"attempt-{turn_id}", node.commitment_id,
        ),
    )


def _project(db, plan, minutes):
    now = T0 + timedelta(minutes=minutes)
    return node_cost.project_many([plan.id], db=db, now=now)[plan.id]


def _overlapping_phase(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _phase(db, plan.id)
    first = _task(db, plan.id, "First", parent_id=phase.id)
    second = _task(db, plan.id, "Second", parent_id=phase.id)
    third = _task(db, plan.id, "Third", parent_id=phase.id)
    _watch(db, first, 0, 60)
    _watch(db, second, 30, 90)
    _turn(db, third, 60, 75)
    db.commit()
    return plan, phase, (first, second, third)


def test_parallel_waits_and_work_count_once_in_the_parent(db):
    plan, phase, (first, second, third) = _overlapping_phase(db)

    costs = _project(db, plan, 200)

    rolled = costs[phase.id]["rolled"]
    # Working: the third child's 15 minutes. Waiting: waits 0-90 minus 60-75.
    assert rolled["execution"]["seconds"] == 15 * MINUTE
    assert rolled["waiting"]["seconds"] == 75 * MINUTE
    # Each child still reads its own interval; own figures stay per node.
    assert costs[first.id]["rolled"]["waiting"]["seconds"] == 60 * MINUTE
    assert costs[second.id]["rolled"]["waiting"]["seconds"] == 60 * MINUTE
    assert costs[third.id]["own"]["execution"]["seconds"] == 15 * MINUTE
    assert costs[phase.id]["own"]["waiting"]["seconds"] == 0


def test_a_leaf_whose_wait_overlaps_its_own_turn_loses_the_overlap_when_rolled(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    leaf = _task(db, plan.id, "Leaf")
    _watch(db, leaf, 0, 40)
    _turn(db, leaf, 30, 50)

    costs = _project(db, plan, 200)[leaf.id]

    assert costs["own"]["waiting"]["seconds"] == 40 * MINUTE
    assert costs["own"]["execution"]["seconds"] == 20 * MINUTE
    assert costs["rolled"]["execution"]["seconds"] == 20 * MINUTE
    assert costs["rolled"]["waiting"]["seconds"] == 30 * MINUTE


def test_a_leaf_without_overlap_reads_the_same_rolled_and_own(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    leaf = _task(db, plan.id, "Leaf")
    _watch(db, leaf, 0, 10)
    _turn(db, leaf, 20, 25)
    _turn(db, leaf, 30, 31)

    costs = _project(db, plan, 200)[leaf.id]

    assert costs["rolled"]["execution"] == costs["own"]["execution"]
    assert costs["rolled"]["waiting"] == costs["own"]["waiting"]


def test_sequential_children_still_add_up(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _phase(db, plan.id)
    left = _task(db, plan.id, "Left", parent_id=phase.id)
    right = _task(db, plan.id, "Right", parent_id=phase.id)
    _turn(db, left, 0, 10)
    _turn(db, right, 20, 25)
    _watch(db, left, 10, 20)
    _watch(db, right, 30, 40)

    rolled = _project(db, plan, 200)[phase.id]["rolled"]

    assert rolled["execution"]["seconds"] == 15 * MINUTE
    assert rolled["waiting"]["seconds"] == 20 * MINUTE


def test_an_attached_plans_intervals_count_once_beside_the_delegating_step(db):
    parent_plan = service.ensure_plan("thread", "owner", db=db)
    delegate = _task(db, parent_plan.id, "Delegate", spawn_kind="claude-code")
    service.transition_node(delegate.id, "active", db=db)
    service.ensure_plan("thread", "child", db=db)
    mutations.attach_spawned_thread(delegate.id, "child", db=db)
    child_plan = db.execute(
        "SELECT * FROM plans WHERE owner_kind='thread' AND owner_id='child'"
    ).fetchone()
    phase = _phase(db, child_plan["id"], "Child phase")
    one = _task(db, child_plan["id"], "One", parent_id=phase.id)
    two = _task(db, child_plan["id"], "Two", parent_id=phase.id)
    _turn(db, one, 10, 30, thread_id="child")
    _turn(db, two, 20, 40, thread_id="child")
    _watch(db, one, 0, 50)
    # The delegating step waited on the worker over the same stretch.
    _watch(db, delegate, 0, 50)
    db.commit()

    costs = _project(db, parent_plan, 200)[delegate.id]

    # Turns 10-30 and 20-40 overlap: 30 minutes. Waits union to 0-50, so 20 of
    # the 50 minutes had a Turn running.
    assert costs["rolled"]["execution"]["seconds"] == 30 * MINUTE
    assert costs["rolled"]["waiting"]["seconds"] == 20 * MINUTE
    assert costs["own"]["waiting"]["seconds"] == 50 * MINUTE
    # Requesting the child plan beside its carrier folds it once, same figures.
    both = node_cost.project_many(
        [parent_plan.id, child_plan["id"]], db=db, now=T0 + timedelta(minutes=200),
    )
    assert both[parent_plan.id][delegate.id]["rolled"] == costs["rolled"]
    assert both[child_plan["id"]][phase.id]["rolled"]["execution"]["seconds"] == 30 * MINUTE


def test_a_parents_time_never_exceeds_the_span_its_descendants_cover(db):
    """Working + Waiting <= the stretch from the earliest to the latest interval."""
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _phase(db, plan.id)
    inner = _phase(db, plan.id, "Inner")
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET parent_id=? WHERE id=?", (phase.id, inner.id))
    leaves = []
    for index in range(12):
        leaf = _task(db, plan.id, f"Leaf {index}", parent_id=(inner if index % 2 else phase).id)
        _watch(db, leaf, index * 3, index * 3 + 40)
        _turn(db, leaf, index * 3 + 10, index * 3 + 25)
        leaves.append(leaf)
    db.commit()
    now_minutes = 300

    costs = _project(db, plan, now_minutes)

    for parent in (phase, inner):
        rolled = costs[parent.id]["rolled"]
        total = rolled["execution"]["seconds"] + rolled["waiting"]["seconds"]
        assert total <= now_minutes * MINUTE
        # The summed figure the old roll-up produced was far past this.
        summed = sum(
            costs[leaf.id]["own"]["waiting"]["seconds"]
            + costs[leaf.id]["own"]["execution"]["seconds"]
            for leaf in leaves
        )
        assert total < summed
    # Every leaf's intervals chain through minutes 0-73, so the phase reads exactly that.
    top = costs[phase.id]["rolled"]
    assert top["execution"]["seconds"] + top["waiting"]["seconds"] == 73 * MINUTE


def test_span_helpers_count_union_and_subtract_working_from_waiting():
    span = {"working": [(10.0, 20.0), (15.0, 30.0)], "waiting": [(0.0, 25.0), (28.0, 40.0)]}
    merged = node_time.merge([span])

    assert merged["working"] == [(10.0, 30.0)]
    assert node_time.seconds(merged) == (20, 10 + 10)


def _select_statements(db, fn) -> int:
    statements: list[str] = []
    db.set_trace_callback(
        lambda statement: (
            statements.append(statement)
            if statement.lstrip().upper().startswith(("SELECT", "WITH"))
            and not statement.rstrip().endswith(") LIMIT 0")
            else None
        )
    )
    try:
        fn()
    finally:
        db.set_trace_callback(None)
    return len(statements)


def _busy_plan(db, owner, width):
    plan = service.ensure_plan("thread", owner, db=db)
    phase = _phase(db, plan.id)
    for index in range(width):
        node = _task(db, plan.id, f"Task {index}", parent_id=phase.id)
        _watch(db, node, index, index + 30)
        _turn(db, node, index + 5, index + 15)
    db.commit()
    return plan


def test_the_roll_up_reads_a_fixed_number_of_queries_at_two_sizes(db):
    small = _busy_plan(db, "owner", 4)
    large = _busy_plan(db, "owner2", 120)

    small_count = _select_statements(db, lambda: node_cost.project_many([small.id], db=db))
    large_count = _select_statements(db, lambda: node_cost.project_many([large.id], db=db))

    assert large_count == small_count
    phase = next(
        row["id"] for row in db.execute(
            "SELECT id FROM plan_nodes WHERE plan_id=? AND kind='phase'", (large.id,),
        )
    )
    spans = node_cost.own_many_timed(
        [row["id"] for row in db.execute("SELECT id FROM plan_nodes WHERE plan_id=?", (large.id,))],
        db=db, now=T0 + timedelta(minutes=500),
    )[1]
    # Merged intervals each node carries: one wait and one Turn per task.
    assert sum(node_time.interval_count(span) for span in spans.values()) == 2 * 120
    rolled = node_cost.project_many([large.id], db=db, now=T0 + timedelta(minutes=500))
    # Turns at minutes i+5..i+15 chain into one stretch, 5..134.
    assert rolled[large.id][phase]["rolled"]["execution"]["seconds"] == 129 * MINUTE
    assert rolled[large.id][phase]["rolled"]["waiting"]["seconds"] == 5 * MINUTE + 15 * MINUTE
