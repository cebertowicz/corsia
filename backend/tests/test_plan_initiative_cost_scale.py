"""The initiative total reads the sharers it sums, not the plans they sit in.

A sharer outside the projected plans (the delegating step a member was
re-attached under) adds its own cost and nothing beneath it, so the plan it sits
in -- and the settled history that plan keeps -- must not be loaded for it.
"""

from __future__ import annotations

from plans import mutations, node_cost, service
from tests.test_plan_initiative_cost import _check_floors, _follow_up, _plan
from tests.test_plan_node_cost import _task, _usage, db  # noqa: F401

UNRELATED = 200


def test_a_sharer_outside_the_loaded_plans_does_not_load_its_whole_plan(db, monkeypatch):
    plan = _plan(db)
    unrelated = [_task(db, plan.id, f"Unrelated settled {index}").id for index in range(UNRELATED)]
    step = _task(db, plan.id, "Delegate", spawn_kind="claude-code")
    service.transition_node(step.id, "active", db=db)
    child_plan = service.ensure_plan("thread", "child", db=db)
    child_task = _task(db, child_plan.id, "Child work")
    child_follow_up = _follow_up(db, child_task)
    mutations.attach_spawned_thread(step.id, "child", db=db)
    child_task = service.get_node(child_task.id, db=db)
    child_follow_up = service.get_node(child_follow_up.id, db=db)
    for index, node in enumerate((step, child_task, child_follow_up)):
        _usage(db, node, tokens=10 ** (index + 1), cost=0.1, usage_id=f"u-{index}",
               thread_id="child" if index else "owner")
    for node_id in unrelated:
        service.transition_node(node_id, "active", db=db)
        service.transition_node(node_id, "failed", db=db)
    db.commit()

    costed: list[str] = []
    real = node_cost.own_many_timed

    def recording(node_ids, **kwargs):
        ids = [str(value) for value in node_ids]
        costed.extend(ids)
        return real(ids, **kwargs)

    monkeypatch.setattr(node_cost, "own_many_timed", recording)

    # The projection asks for the child plan alone: the delegating step is a
    # sharer outside it, in a plan that also holds the unrelated history.
    _check_floors(db, [child_plan], [child_task, child_follow_up], expected=1110)

    assert step.id in costed
    assert not set(unrelated) & set(costed)
    assert len(costed) == len(set(costed)) <= 4
