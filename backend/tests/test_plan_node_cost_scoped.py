"""A node's rolled cost read from its subtree alone equals the full-plan figure (#731 S2-1).

``node_cost_scoped.project`` serves the agent's single-node read without rolling the
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
        assert _rolled(db, node.id, now) == full[node.id]["rolled"], node.id
    # The overlap is merged, not added: the phase's Working is the union of three Turns.
    assert full[phase.id]["rolled"]["execution"]["seconds"] == (30 + 10) * 60
    assert full[phase.id]["rolled"]["waiting"]["seconds"] > 0


def _rolled(db, node_id, now):
    """One node's rolled cost, read from its subtree alone."""
    return node_cost_scoped.project([], [node_id], (), db=db, now=now)[1][node_id]


def _initiatives(db, node_ids):
    """Each node's initiative total, read from the nodes' subtrees and lineages alone."""
    return node_cost_scoped.project([], node_ids, (), db=db)[2]


def _usage_tokens(db, node, tokens):
    from tests.test_plan_node_cost import _usage
    _usage(db, node, tokens=tokens, cost=0.01, usage_id=f"u-scoped-{node.id}")


def test_a_scoped_initiative_under_a_phase_equals_the_full_plan_figure(db):
    """A lineage nested under a phase: the phase is outside the loaded subtree but in the plan.

    Its commitment root (the phase) is read from outside; merging it must add to the plan's
    loaded rows, not replace them, or the members vanish from the lookup.
    """
    from plans import followups

    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _phase(db, plan.id)
    member = _task(db, plan.id, "Member", parent_id=phase.id)
    sibling = _task(db, plan.id, "Unrelated sibling", parent_id=phase.id)
    follow_up = followups.add_follow_up(member.id, title="Follow-up", acceptance_criteria="d", db=db)
    for node, tokens in ((member, 10), (follow_up, 100), (sibling, 100000)):
        _usage_tokens(db, node, tokens)
    db.commit()

    _by_plan, full = node_cost.project_with_initiatives([plan.id], db=db)
    scoped = _initiatives(db, [member.id, follow_up.id, sibling.id])
    for node in (member, follow_up, sibling):
        assert scoped[node.id] == full[node.id], node.id
    assert scoped[member.id]["cost"]["tokens"]["known"] == 110


def test_a_scoped_initiative_with_a_same_plan_split_sibling_equals_the_full_plan_figure(db):
    """A commitment sharer in the member's own plan, outside the members' subtrees."""
    from plans import followups

    plan = service.ensure_plan("thread", "owner", db=db)
    root = _task(db, plan.id, "Root")
    follow_up = followups.add_follow_up(root.id, title="Follow-up", acceptance_criteria="d", db=db)
    split = mutations.split_node(follow_up.id, title="Split", acceptance_criteria="d", db=db)
    for node, tokens in ((root, 10), (follow_up, 100), (split, 1000)):
        _usage_tokens(db, node, tokens)
    db.commit()

    _by_plan, full = node_cost.project_with_initiatives([plan.id], db=db)
    scoped = _initiatives(db, [root.id, follow_up.id, split.id])
    for node in (root, follow_up, split):
        assert scoped[node.id] == full[node.id], node.id
