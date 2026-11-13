"""A node's rolled cost read from its subtree alone equals the full-plan figure (#731 S2-1).

``node_cost_scoped.project`` serves the agent's single-node read without rolling the
whole plan; Working and Waiting are interval unions (#893), so it must carry the same
spans the full roll carries, including an attached child plan's.
"""

from __future__ import annotations

from datetime import timedelta

from plans import mutations, node_cost, node_cost_scoped, node_time, service
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


def test_working_seconds_equals_the_scoped_rolled_execution_seconds_per_node(db):
    """``working_seconds`` skips the ledger, failure, waiting and initiative reads; the seconds are ``project``'s.

    The roll carries overlapping Turns (merged, not added), a Turn still running (counted to the
    clock), a Turn that ended before it started (unknown, no seconds), an attached child plan,
    and a node that also has usage rows and a wait, which the lean read leaves out.
    """
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _phase(db, plan.id)
    first = _task(db, plan.id, "First", parent_id=phase.id)
    second = _task(db, plan.id, "Second", parent_id=phase.id)
    idle = _task(db, plan.id, "Idle", parent_id=phase.id)
    delegate = _task(db, plan.id, "Delegate", parent_id=phase.id, spawn_kind="claude-code")
    service.transition_node(delegate.id, "active", db=db)
    service.ensure_plan("thread", "child", db=db)
    mutations.attach_spawned_thread(delegate.id, "child", db=db)
    child_plan = db.execute("SELECT * FROM plans WHERE owner_kind='thread' AND owner_id='child'").fetchone()
    inner = _phase(db, child_plan["id"], "Inner")
    one = _task(db, child_plan["id"], "One", parent_id=inner.id)
    two = _task(db, child_plan["id"], "Two", parent_id=inner.id)
    _watch(db, first, 0, 60)
    _usage_tokens(db, first, 500)
    _turn(db, first, 70, 80)  # 10 min
    _turn(db, second, 75, 90)  # 15 min, overlaps the first by 5
    _turn(db, one, 10, 30, thread_id="child")  # 20 min
    _turn(db, two, 20, 40, thread_id="child")  # 20 min, overlaps `one` by 10
    _turn(db, idle, 50, 40)  # ended before it started: unknown, adds nothing
    db.execute(
        """INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch,
               plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role)
           VALUES ('running-turn','owner',0,'test-model','running',?,'boot',?,?,'attempt-run',?,'execution')""",
        ((T0 + timedelta(minutes=100)).strftime("%Y-%m-%dT%H:%M:%SZ"), plan.id, idle.id, idle.commitment_id),
    )
    db.commit()

    now = T0 + timedelta(minutes=110)
    nodes = [phase.id, first.id, second.id, idle.id, delegate.id]
    lean = node_cost_scoped.working_seconds(nodes, db=db, now=now)
    full = node_cost_scoped.project([], nodes, (), db=db, now=now)[1]
    assert lean == {node_id: full[node_id]["execution"]["seconds"] for node_id in nodes}
    assert lean[first.id] == 10 * 60
    assert lean[second.id] == 15 * 60
    assert lean[idle.id] == 10 * 60  # the running Turn, 100 to 110 min; the unknown Turn adds none
    assert lean[delegate.id] == 30 * 60  # union of 10-30 and 20-40 in the child plan
    assert lean[phase.id] == 20 * 60 + 30 * 60 + 10 * 60  # 70-90 merged, the child's 30, the running 10


def test_working_seconds_rolls_a_nested_delegation_and_matches_the_full_plan_per_node(db):
    """Two plans deep, siblings and a repeated Turn: each wanted node's seconds are the concrete union below.

    The roll that skips the totals must carry a grandchild plan's span through the child plan's
    delegating step, merge overlapping siblings once, and equal ``project_many``'s rolled figure.
    """
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _phase(db, plan.id)
    sibling = _task(db, plan.id, "Sibling", parent_id=phase.id)
    first = _task(db, plan.id, "Delegate one", parent_id=phase.id, spawn_kind="claude-code")
    service.transition_node(first.id, "active", db=db)
    service.ensure_plan("thread", "child", db=db)
    mutations.attach_spawned_thread(first.id, "child", db=db)
    child = db.execute("SELECT * FROM plans WHERE owner_kind='thread' AND owner_id='child'").fetchone()
    inner = _phase(db, child["id"], "Inner")
    deeper = _task(db, child["id"], "Deeper delegate", parent_id=inner.id, spawn_kind="claude-code")
    service.transition_node(deeper.id, "active", db=db)
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,spawned_by,model,created_at,updated_at) "
        "VALUES ('grandchild','user-1','project-1','child','test/model','t','t')"
    )
    service.ensure_plan("thread", "grandchild", db=db)
    mutations.attach_spawned_thread(deeper.id, "grandchild", db=db)
    grand = db.execute("SELECT * FROM plans WHERE owner_kind='thread' AND owner_id='grandchild'").fetchone()
    leaf = _task(db, grand["id"], "Leaf")
    _turn(db, leaf, 0, 20, thread_id="grandchild")  # 20 min, two plans below the step
    _turn(db, leaf, 10, 30, thread_id="grandchild")  # overlaps it: the leaf's union is 0-30
    _turn(db, inner, 25, 50, thread_id="child")  # 25 min, overlaps the leaf's last 5
    _turn(db, sibling, 100, 110)  # 10 min, apart from the rest
    _turn(db, sibling, 100, 105)  # inside the first
    db.commit()

    now = T0 + timedelta(minutes=200)
    nodes = [phase.id, sibling.id, first.id]
    lean = node_cost_scoped.working_seconds(nodes, db=db, now=now)
    full = node_cost.project_many([plan.id], db=db, now=now)[plan.id]
    assert lean[first.id] == 50 * 60  # 0-30 from the grandchild, merged with 25-50 in the child plan
    assert lean[sibling.id] == 10 * 60
    assert lean[phase.id] == (50 + 10) * 60
    for node_id in nodes:
        assert lean[node_id] == full[node_id]["rolled"]["execution"]["seconds"], node_id


def test_the_working_only_read_returns_the_working_spans_of_the_full_read(db):
    """Reading Working alone gives each node the same Working span the full own-facts read does, and no waiting."""
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Only")
    quiet = _task(db, plan.id, "No Turns")
    _turn(db, node, 0, 10)
    _turn(db, node, 5, 20)
    db.commit()
    now = T0 + timedelta(minutes=60)
    spans = node_cost._own_many_working_timed([node.id, quiet.id], db=db, now=now)
    own, full_spans = node_cost.own_many_timed([node.id, quiet.id], db=db, now=now)
    assert spans == {node.id: {"working": full_spans[node.id]["working"], "waiting": []}}
    assert own[node.id]["execution"]["seconds"] == 25 * 60  # the Turns add; the span is their union
    assert node_time.seconds(spans[node.id])[0] == 20 * 60


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
