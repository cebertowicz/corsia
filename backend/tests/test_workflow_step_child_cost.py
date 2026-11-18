"""A step's scoped roll-up includes the work its sub-task did."""

import pytest

from plans import node_cost_scoped, service
from tests.test_plan_node_cost import db, _price, _priced_usage, _task, _turn  # noqa: F401


def test_step_rolls_child_cost_and_time(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = service.add_node(plan.id, kind="phase", title="Run", acceptance_criteria="Done", db=db)
    step = _task(db, plan.id, "Step", parent_id=phase.id, workflow_step="draft", difficulty="routine")
    child = _task(db, plan.id, "Child", parent_id=step.id)
    _priced_usage(db, child, tokens=100, usage_id="child-usage")
    _turn(db, child, "child-turn", started="2026-01-01T00:00:00+00:00", ended="2026-01-01T00:00:10+00:00")

    own, rolled, _initiatives, _ledger = node_cost_scoped.project([step.id], [step.id], (), db=db)

    assert own[step.id]["tokens"]["known"] == 0
    assert rolled[step.id]["tokens"]["known"] == 100
    assert rolled[step.id]["model_cost_usd"]["known"] == pytest.approx(_price(100))
    assert rolled[step.id]["execution"]["seconds"] == 10
