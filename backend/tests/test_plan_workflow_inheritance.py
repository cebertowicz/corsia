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


@pytest.mark.parametrize("retire", ["close", "archive"])
def test_retiring_a_phase_prerequisite_releases_its_open_steps(db, retire):
    from plans import archival, closure

    plan = owner_plan(db)
    before = _node(db, plan, "Before")
    phase = _node(db, plan, "Phase", "phase")
    step = _node(db, plan, "Step", parent=phase.id)
    service.add_dependency(phase.id, before.id, db=db)
    assert service.get_node(step.id, db=db).unmet_dependencies == [before.id]

    if retire == "close":
        closure.close_node(before.id, reason="Not needed", db=db)
    else:
        archival.archive_node(before.id, db=db)

    assert service.get_node(phase.id, db=db).unmet_dependencies == []
    assert service.get_node(step.id, db=db).unmet_dependencies == []


def test_step_blocked_events_follow_its_own_edges_while_its_phase_waits(db):
    def events(node_id):
        return [r[0] for r in db.execute(
            "SELECT event FROM plan_node_events WHERE node_id=? AND event LIKE 'blocked_%' ORDER BY rowid", (node_id,),
        )]

    plan = owner_plan(db)
    own = _node(db, plan, "Own")
    outer = _node(db, plan, "Outer")
    phase = _node(db, plan, "Phase", "phase")
    step = _node(db, plan, "Step", parent=phase.id)
    service.add_dependency(step.id, own.id, db=db)
    service.add_dependency(phase.id, outer.id, db=db)
    _mark_done(db, plan.id, own.id)

    assert events(step.id) == ["blocked_raised", "blocked_cleared"]
    assert service.get_node(step.id, db=db).unmet_dependencies == [outer.id]
    _mark_done(db, plan.id, outer.id)
    assert events(step.id) == ["blocked_raised", "blocked_cleared"]
    assert service.get_node(step.id, db=db).unmet_dependencies == []


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
    # The inherited edge is the ancestor's; removing it from the child changes nothing.
    assert service.remove_dependency(child.id, before.id, db=db) is False
