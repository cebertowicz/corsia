"""Regression cases for bulk mutation review."""

import pytest

from plans import service
from tests.plan_close_next_helpers import HUMAN, owner_plan, plan_update, row
from tests.test_plan_bulk_edit import _edits, _node, _phase
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401


@pytest.mark.parametrize("change", ["regroup", "later_false"])
def test_bulk_edit_keeps_slot_unless_it_moves_into_later(db, change):
    plan = owner_plan(db)
    branch = _phase(db, plan.id)
    active = _node(db, plan.id, "Active")
    plan_update(db, plan.id, "activate", {"node_id": active.id, "expected_revision": active.revision})
    params = {"parent_id": branch.id} if change == "regroup" else {"later": False}

    result = _edits(db, plan.id, [active.id], **params)

    assert "next" not in result
    assert db.execute("SELECT 1 FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL", (active.id,)).fetchone()


def test_user_bulk_close_is_refused_at_boundary(db):
    plan = owner_plan(db)
    task = _node(db, plan.id, "Task")
    with pytest.raises(service.PlanInputError, match="user.*bulk close"):
        plan_update(db, plan.id, "close_node", {"node_ids": [task.id], "reason": "Skip"}, actor=HUMAN)
    assert row(db, task.id)["state"] == "pending"


def test_regroup_refuses_cycle_through_archived_ancestor(db):
    plan = owner_plan(db)
    top = _phase(db, plan.id, "Top")
    archived = _phase(db, plan.id, "Archived", parent_id=top.id)
    descendant = _phase(db, plan.id, "Descendant", parent_id=archived.id)
    from plans import aggregates_rebuild

    service.touch_node(db, archived.id, at=service._now(), archived_at=service._now())
    aggregates_rebuild.rebuild(db, plan.id)
    db.commit()

    from plans.regroup import _refuse

    with pytest.raises(service.PlanInputError, match="own branch"):
        _refuse(db, row(db, top.id), descendant.id)
    assert row(db, top.id)["parent_id"] is None
