"""Inherited dependencies keep nested work ordered without moving edges."""

import pytest

from plans import node_waits, service
from tests.plan_close_next_helpers import owner_plan
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401
from tests.test_plan_user_task_mark_done import _mark_done


def _node(db, plan, title, kind="task", parent=None):
    return service.add_node(plan.id, kind=kind, title=title, acceptance_criteria="Done", parent_id=parent, db=db)


def test_settling_child_keeps_ancestor_edge_and_sibling_blocked(db):
    plan = owner_plan(db)
    before = _node(db, plan, "Before")
    phase = _node(db, plan, "Phase", "phase")
    done = _node(db, plan, "Done", parent=phase.id)
    sibling = _node(db, plan, "Sibling", parent=phase.id)
    service.add_dependency(phase.id, before.id, db=db)

    result = _mark_done(db, plan.id, done.id)
    assert result["node"]["state"] == "accepted"
    assert db.execute("SELECT 1 FROM plan_node_dependencies WHERE node_id=? AND depends_on_node_id=?", (phase.id, before.id)).fetchone()
    assert service.get_node(sibling.id, db=db).unmet_dependencies == [before.id]


def test_inherited_rows_deduplicate_prefer_nearest_and_allow_direct_removal(db):
    plan = owner_plan(db)
    before = _node(db, plan, "Before")
    other = _node(db, plan, "Other")
    outer = _node(db, plan, "Outer", "phase")
    inner = _node(db, plan, "Inner", "phase", outer.id)
    child = _node(db, plan, "Child", parent=inner.id)
    service.add_dependency(outer.id, before.id, db=db)
    service.add_dependency(inner.id, before.id, db=db)
    service.add_dependency(inner.id, other.id, db=db)
    service.add_dependency(child.id, before.id, db=db)
    for compact in (False, True):
        rows = node_waits.unmet_prerequisites(db, [child.id], compact=compact)[child.id]
        assert [(r["id"], r["via"]) for r in rows] == [(before.id, None), (other.id, inner.id)]
    assert service.remove_dependency(child.id, before.id, db=db)
    rows = node_waits.unmet_prerequisites(db, [child.id])[child.id]
    assert [(r["id"], r["via"]) for r in rows] == [(before.id, inner.id), (other.id, inner.id)]
    with pytest.raises(service.PlanInputError, match=inner.id):
        service.remove_dependency(child.id, before.id, db=db)
