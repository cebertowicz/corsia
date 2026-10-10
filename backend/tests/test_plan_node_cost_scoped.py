"""A node's rolled cost read from its subtree alone equals the full-plan figure (#731 S2-1).

``node_cost_scoped.rolled_of`` serves the agent's single-node read without rolling the
whole plan; Working and Waiting are interval unions (#893), so it must carry the same
spans the full roll carries, including an attached child plan's.
"""

from __future__ import annotations

from datetime import timedelta

from plans import mutations, node_cost, node_cost_scoped, service
from tests.test_plan_node_time_rollup import (  # noqa: F401  (db is the fixture)
    T0, _phase, _task, _turn, _watch, db,
)


def test_a_scoped_rolled_figure_equals_the_full_plan_figure_time_included(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _phase(db, plan.id)
    first = _task(db, plan.id, "First", parent_id=phase.id)
    second = _task(db, plan.id, "Second", parent_id=phase.id)
    delegate = _task(db, plan.id, "Delegate", parent_id=phase.id, spawn_kind="claude-code")
    service.transition_node(delegate.id, "active", db=db)
    service.ensure_plan("thread", "child", db=db)
    mutations.attach_spawned_thread(delegate.id, "child", db=db)
    child_plan = db.execute(
        "SELECT * FROM plans WHERE owner_kind='thread' AND owner_id='child'"
    ).fetchone()
    inner = _phase(db, child_plan["id"], "Inner")
    one = _task(db, child_plan["id"], "One", parent_id=inner.id)
    two = _task(db, child_plan["id"], "Two", parent_id=inner.id)
    _watch(db, first, 0, 60)
    _watch(db, second, 30, 90)  # overlaps the first wait
    _watch(db, delegate, 0, 100)
    _turn(db, one, 10, 30, thread_id="child")
    _turn(db, two, 20, 40, thread_id="child")  # overlaps the first turn
    _turn(db, first, 70, 80)
    db.commit()

    now = T0 + timedelta(minutes=200)
    full = node_cost.project_many([plan.id], db=db, now=now)[plan.id]
    for node in (phase, first, second, delegate):
        assert node_cost_scoped.rolled_of(node.id, db=db, now=now) == full[node.id]["rolled"], node.id
    # The overlap is merged, not added: the phase's Working is the union of three Turns.
    assert full[phase.id]["rolled"]["execution"]["seconds"] == (30 + 10) * 60
    assert full[phase.id]["rolled"]["waiting"]["seconds"] > 0
