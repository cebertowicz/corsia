"""The initiative total over a follow-up lineage (C1, design 2.7 cases a-i).

Fail-first: ``node_cost.project_with_initiatives`` and the operator node's
``initiative`` field do not exist before this change. Every membership case
asserts the same two floors: the total is never below the older "With
revisions and follow-ups" figure (the commitment total) and never below any
member's rolled Tokens line.
"""

from __future__ import annotations

import pytest

from plans import commitment_cost, followups, mutations, node_cost, operator_node_state, rollup, service
from tests.test_plan_node_cost import _select_statements, _task, _turn, _usage, db  # noqa: F401


def _plan(db, owner="owner"):
    return service.ensure_plan("thread", owner, db=db)


def _follow_up(db, source, title="Follow-up"):
    return followups.add_follow_up(
        source.id, title=title, acceptance_criteria=f"{title} DOD", db=db,
    )


def _project(db, plan_ids):
    by_plan, initiatives = node_cost.project_with_initiatives(
        [plan.id if hasattr(plan, "id") else plan for plan in plan_ids], db=db,
    )
    return by_plan, initiatives


def _tokens(amount) -> int | None:
    return amount["tokens"]["known"]


def _check_floors(db, plan_ids, members, expected):
    """The total is ``expected``, one value on every member, above both floors."""
    by_plan, initiatives = _project(db, plan_ids)
    totals = {_tokens(initiatives[member.id]["cost"]) for member in members}
    assert totals == {expected}
    for member in members:
        assert initiatives[member.id]["follow_ups"] == len(members) - 1
        rolled = next(
            costs[member.id]["rolled"] for costs in by_plan.values() if member.id in costs
        )
        assert _tokens(rolled) <= expected
        if member.commitment_id == member.id:
            older = commitment_cost.project(member.commitment_id, db=db)
            assert (older["tokens"]["known"] or 0) <= expected
    return initiatives


def _phase(db, plan_id, title="Root"):
    return service.add_node(plan_id, kind="phase", title=title, acceptance_criteria="d", db=db)


def _chain(db, plan, count, *, tokens=10):
    nodes = [_task(db, plan.id, "Root")]
    for index in range(count):
        nodes.append(_follow_up(db, nodes[-1], f"Follow-up {index + 1}"))
    for index, node in enumerate(nodes):
        _usage(db, node, tokens=tokens, cost=0.01, usage_id=f"u-chain-{node.id}")
    return nodes


def test_a_a_parent_with_sub_steps_and_no_follow_ups_has_no_initiative(db):
    plan = _plan(db)
    parent = service.add_node(plan.id, kind="phase", title="P", acceptance_criteria="d", db=db)
    child = _task(db, plan.id, "Sub", parent_id=parent.id)
    _usage(db, child, tokens=50, cost=0.05, usage_id="u-sub")

    _by_plan, initiatives = _project(db, [plan])

    assert initiatives[parent.id] is None
    assert initiatives[child.id] is None


def test_b_branches_count_every_member_once_with_one_value_everywhere(db):
    plan = _plan(db)
    root = _task(db, plan.id, "Root")
    left, right = _follow_up(db, root, "Left"), _follow_up(db, root, "Right")
    for index, node in enumerate((root, left, right)):
        _usage(db, node, tokens=100 * (index + 1), cost=0.1, usage_id=f"u-{index}")

    _check_floors(db, [plan], [root, left, right], expected=600)


def test_chain_past_the_old_depth_cap_is_one_initiative(db):
    plan = _plan(db)
    nodes = _chain(db, plan, 6)

    initiatives = _check_floors(db, [plan], nodes, expected=70)

    assert initiatives[nodes[-1].id]["follow_ups"] == 6


def test_c_a_rejected_delegated_step_counts_its_worker_once(db):
    from plans import rejection
    from tests.test_plan_node_cost import _submit

    plan = _plan(db)
    step = _task(db, plan.id, "Delegate", spawn_kind="claude-code")
    service.transition_node(step.id, "active", db=db)
    service.ensure_plan("thread", "child", db=db)
    mutations.attach_spawned_thread(step.id, "child", db=db)
    child_plan = db.execute("SELECT * FROM plans WHERE owner_kind='thread' AND owner_id='child'").fetchone()
    child_task = _task(db, child_plan["id"], "Child work")
    _usage(db, child_task, tokens=500, cost=0.5, usage_id="u-child", thread_id="child")
    _usage(db, step, tokens=7, cost=0.01, usage_id="u-step")
    _submit(db, step.id, actor="owner")
    _rejected, revision, _ = rejection.reject_submission(
        step.id, feedback="Not yet", actor_id="owner", db=db,
    )
    _usage(db, revision, tokens=11, cost=0.01, usage_id="u-revision")

    _check_floors(db, [plan], [step, revision], expected=518)


def test_d_a_split_sibling_of_a_member_is_counted_on_its_own_cost(db):
    plan = _plan(db)
    root = _task(db, plan.id, "Root")
    follow_up = _follow_up(db, root)
    sibling = mutations.split_node(
        follow_up.id, title="Split", acceptance_criteria="Split DOD", db=db,
    )
    for index, node in enumerate((root, follow_up, sibling)):
        _usage(db, node, tokens=10 ** (index + 1), cost=0.1, usage_id=f"u-{index}")

    _by_plan, initiatives = _project(db, [plan])

    assert _tokens(initiatives[root.id]["cost"]) == 10 + 100 + 1000
    assert initiatives[sibling.id] is None


def test_e_a_re_attached_delegated_node_is_counted_once_from_either_view(db):
    """The lineage lives in the worker's plan; its delegating step shares the commitment."""
    plan = _plan(db)
    step = _task(db, plan.id, "Delegate", spawn_kind="claude-code")
    service.transition_node(step.id, "active", db=db)
    child_plan = service.ensure_plan("thread", "child", db=db)
    child_task = _task(db, child_plan.id, "Child work")
    child_follow_up = _follow_up(db, child_task)
    mutations.attach_spawned_thread(step.id, "child", db=db)
    child_task = service.get_node(child_task.id, db=db)
    child_follow_up = service.get_node(child_follow_up.id, db=db)
    assert child_task.commitment_id == step.commitment_id == child_follow_up.commitment_id
    for index, node in enumerate((step, child_task, child_follow_up)):
        _usage(db, node, tokens=10 ** (index + 1), cost=0.1, usage_id=f"u-{index}",
               thread_id="child" if index else "owner")

    both = _check_floors(db, [plan], [child_task, child_follow_up], expected=1110)
    child_only = _check_floors(db, [child_plan], [child_task, child_follow_up], expected=1110)

    assert both[child_task.id] == child_only[child_task.id]


def test_f_a_subtree_node_sharing_a_members_commitment_is_counted_once(db):
    plan = _plan(db)
    root = service.add_node(plan.id, kind="phase", title="Root", acceptance_criteria="d", db=db)
    child = _task(db, plan.id, "Sub", parent_id=root.id)
    with service._write(db):
        service.touch_node(db, child.id, commitment_id=root.commitment_id)
    follow_up = _follow_up(db, root)
    for index, node in enumerate((root, child, follow_up)):
        _usage(db, node, tokens=10 ** (index + 1), cost=0.1, usage_id=f"u-{index}")
    db.commit()

    _check_floors(db, [plan], [root, follow_up], expected=1110)


def test_g_a_delegating_member_counts_its_child_plan_steps_once(db):
    plan = _plan(db)
    root = _task(db, plan.id, "Delegate", spawn_kind="claude-code")
    service.transition_node(root.id, "active", db=db)
    follow_up = _follow_up(db, root)
    child_plan = service.ensure_plan("thread", "child", db=db)
    ran = _task(db, child_plan.id, "Ran before attach")
    _usage(db, ran, tokens=300, cost=0.3, usage_id="u-ran", thread_id="child")
    mutations.attach_spawned_thread(root.id, "child", db=db)
    later = _task(db, child_plan.id, "Created after attach")
    _usage(db, later, tokens=40, cost=0.04, usage_id="u-later", thread_id="child")
    _usage(db, follow_up, tokens=5, cost=0.01, usage_id="u-follow")

    _check_floors(db, [plan], [root, follow_up], expected=345)


def test_h_archived_follow_up_and_archived_sub_step_are_counted(db):
    plan = _plan(db)
    root = _phase(db, plan.id)
    sub = _task(db, plan.id, "Sub", parent_id=root.id)
    follow_up = _follow_up(db, root)
    for index, node in enumerate((root, sub, follow_up)):
        _usage(db, node, tokens=10 ** (index + 1), cost=0.1, usage_id=f"u-{index}")
    db.execute("UPDATE plan_nodes SET archived_at='t' WHERE id IN (?,?)", (sub.id, follow_up.id))
    db.commit()

    _by_plan, initiatives = _project(db, [plan])

    assert _tokens(initiatives[root.id]["cost"]) == 1110


def test_i_a_follow_up_of_a_sub_step_is_not_counted_twice(db):
    plan = _plan(db)
    root = _phase(db, plan.id)
    sub = _task(db, plan.id, "Sub", parent_id=root.id)
    sub_follow_up = _follow_up(db, sub, "Sub follow-up")
    root_follow_up = _follow_up(db, root, "Root follow-up")
    for index, node in enumerate((root, sub, sub_follow_up, root_follow_up)):
        _usage(db, node, tokens=10 ** index, cost=0.1, usage_id=f"u-{index}")

    outer = _check_floors(db, [plan], [root, root_follow_up], expected=1111)
    # A sub-step shares its parent's commitment, which is not membership.
    inner = _check_floors(db, [plan], [sub, sub_follow_up], expected=110)

    assert outer[root.id]["follow_ups"] == inner[sub.id]["follow_ups"] == 1


def test_a_total_is_unknown_not_a_lower_number_when_usage_was_not_recorded(db):
    plan = _plan(db)
    root = _task(db, plan.id, "Root")
    follow_up = _follow_up(db, root)
    _usage(db, root, tokens=10, cost=0.1, usage_id="u-ok")
    _usage(db, follow_up, tokens=None, cost=None, usage_id="u-unknown")

    _by_plan, initiatives = _project(db, [plan])

    cost = initiatives[root.id]["cost"]
    assert cost["tokens"] == {"known": 10, "unknown_rows": 1}


def test_open_is_true_until_every_member_is_settled_or_archived(db):
    plan = _plan(db)
    root = _task(db, plan.id, "Root")
    follow_up = _follow_up(db, root)

    assert _project(db, [plan])[1][root.id]["open"] is True

    db.execute("UPDATE plan_nodes SET state='accepted' WHERE id=?", (root.id,))
    db.execute("UPDATE plan_nodes SET archived_at='t' WHERE id=?", (follow_up.id,))
    assert _project(db, [plan])[1][root.id]["open"] is False


def test_the_operator_node_carries_the_field_and_null_elsewhere(db):
    plan = _plan(db)
    root = _task(db, plan.id, "Root")
    follow_up = _follow_up(db, root)
    alone = _task(db, plan.id, "Alone")
    _usage(db, root, tokens=3, cost=0.1, usage_id="u-1")
    _usage(db, follow_up, tokens=4, cost=0.1, usage_id="u-2")

    states = operator_node_state.project([rollup.rollup_thread(plan.id, db=db)], {}, db=db)

    assert states[root.id]["initiative"] == states[follow_up.id]["initiative"]
    assert states[root.id]["initiative"]["follow_ups"] == 1
    assert _tokens(states[root.id]["initiative"]["cost"]) == 7
    assert states[alone.id]["initiative"] is None
    lean = operator_node_state.project(
        [rollup.rollup_thread(plan.id, db=db)], {}, db=db, costs=False,
    )
    assert lean[root.id]["initiative"] is None


def test_query_count_does_not_grow_with_lineage_size(db):
    small, large = _plan(db, "owner"), _plan(db, "owner2")
    _chain(db, small, 2)
    _chain(db, large, 40)

    small_count = _select_statements(db, lambda: node_cost.project_with_initiatives([small.id], db=db))
    large_count = _select_statements(db, lambda: node_cost.project_with_initiatives([large.id], db=db))

    assert large_count == small_count


def test_a_plan_without_a_lineage_adds_no_query(db):
    plan = _plan(db)
    for index in range(5):
        _usage(db, _task(db, plan.id, f"Task {index}"), tokens=1, cost=0.1, usage_id=f"u-{index}")

    plain = _select_statements(db, lambda: node_cost.project_many([plan.id], db=db))
    with_initiatives = _select_statements(
        db, lambda: node_cost.project_with_initiatives([plan.id], db=db),
    )

    assert with_initiatives == plain


def test_lineages_add_a_fixed_number_of_queries_however_many_there_are(db):
    one, many = _plan(db, "owner"), _plan(db, "owner2")
    _chain(db, one, 3)
    for _ in range(12):
        _chain(db, many, 3)

    base = _select_statements(db, lambda: node_cost.project_many([one.id], db=db))
    one_count = _select_statements(db, lambda: node_cost.project_with_initiatives([one.id], db=db))
    many_count = _select_statements(db, lambda: node_cost.project_with_initiatives([many.id], db=db))

    assert many_count == one_count
    assert one_count - base == 1


def test_nested_lineage_excludes_unrelated_phase_siblings(db):
    """Descendants inherit the phase's commitment; sharing it is not membership."""
    plan = _plan(db)
    phase = _phase(db, plan.id, "Phase")
    member = _task(db, plan.id, "Member", parent_id=phase.id)
    sibling = _task(db, plan.id, "Unrelated sibling", parent_id=phase.id)
    follow_up = _follow_up(db, member)
    _usage(db, member, tokens=10, cost=0.01, usage_id="u-m")
    _usage(db, follow_up, tokens=100, cost=0.01, usage_id="u-f")
    _usage(db, sibling, tokens=100000, cost=1.0, usage_id="u-s")

    _by_plan, initiatives = _project(db, [plan])

    assert _tokens(initiatives[member.id]["cost"]) == 110
    assert initiatives[sibling.id] is None


def _delegate(db, plan, title="Delegate"):
    step = _task(db, plan.id, title, spawn_kind="claude-code")
    service.transition_node(step.id, "active", db=db)
    return step


def test_worker_lineage_excludes_unrelated_worker_tasks(db):
    """The re-attached root counts its own cost, never the plan it opens."""
    plan = _plan(db)
    step = _delegate(db, plan)
    child_plan = service.ensure_plan("thread", "child", db=db)
    member = _task(db, child_plan.id, "Child work")
    sibling = _task(db, child_plan.id, "Unrelated worker task")
    follow_up = _follow_up(db, member)
    mutations.attach_spawned_thread(step.id, "child", db=db)
    _usage(db, step, tokens=1, cost=0.01, usage_id="u-step")
    _usage(db, member, tokens=10, cost=0.01, usage_id="u-m", thread_id="child")
    _usage(db, follow_up, tokens=100, cost=0.01, usage_id="u-f", thread_id="child")
    _usage(db, sibling, tokens=100000, cost=1.0, usage_id="u-s", thread_id="child")

    _by_plan, initiatives = _project(db, [child_plan])

    assert _tokens(initiatives[member.id]["cost"]) == 111


def test_a_phase_in_the_parent_plan_is_not_a_re_attached_root(db):
    plan = _plan(db)
    phase = _phase(db, plan.id, "Parent phase")
    step = _task(db, plan.id, "Delegate", parent_id=phase.id, spawn_kind="claude-code")
    service.transition_node(step.id, "active", db=db)
    child_plan = service.ensure_plan("thread", "child", db=db)
    member = _task(db, child_plan.id, "Child work")
    follow_up = _follow_up(db, member)
    mutations.attach_spawned_thread(step.id, "child", db=db)
    _usage(db, phase, tokens=1000, cost=0.1, usage_id="u-p")
    _usage(db, member, tokens=10, cost=0.01, usage_id="u-m", thread_id="child")
    _usage(db, follow_up, tokens=100, cost=0.01, usage_id="u-f", thread_id="child")

    _by_plan, initiatives = _project(db, [plan, child_plan])

    assert _tokens(initiatives[member.id]["cost"]) == 110
