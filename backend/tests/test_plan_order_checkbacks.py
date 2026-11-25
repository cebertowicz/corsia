"""Order holds count an agent's own deadline check-back above a named start."""

from datetime import datetime, timedelta, timezone

import pytest

from plans import service
from tests.plan_close_next_helpers import HUMAN, owner_plan, plan_update, queue_active, revision, task
from tests.plan_scene import _artifact
from tests.test_plan_reconcile_release import db  # noqa: F401


@pytest.mark.parametrize("reason", ["Revisit the plan", None], ids=["scheduled", "no-reason"])
def test_checkback_above_refuses_lower_start(db, reason):
    plan = owner_plan(db)
    top = task(db, plan.id, "Top check-back")
    lower = task(db, plan.id, "Lower work")
    fields = {
        "node_id": top.id, "expected_revision": revision(db, top.id),
        "condition": {"type": "deadline", "value": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()},
    }
    if reason is None:
        fields["expected_event"] = "test.checkback"
    else:
        fields["reason"] = reason
    plan_update(db, plan.id, "arm_watch", fields)
    with pytest.raises(service.PlanError, match="Top check-back"):
        plan_update(db, plan.id, "start", {"node_id": lower.id, "expected_revision": revision(db, lower.id)})


def _start(db, plan_id, node, *, actor=None):
    kwargs = {} if actor is None else {"actor": actor}
    return plan_update(db, plan_id, "start", {
        "node_id": node.id, "expected_revision": revision(db, node.id),
    }, **kwargs)


def _deadline(db, plan_id, node):
    plan_update(db, plan_id, "arm_watch", {
        "node_id": node.id, "expected_revision": revision(db, node.id),
        "condition": {"type": "deadline", "value": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()},
        "expected_event": "test.checkback",
    })


def test_external_block_and_deadline_do_not_prevent_lower_start(db):
    plan = owner_plan(db)
    top = task(db, plan.id, "Wait for vendor")
    lower = task(db, plan.id, "Lower work")
    plan_update(db, plan.id, "block_node", {
        "node_id": top.id, "expected_revision": revision(db, top.id),
        "owner_kind": "external", "reason_key": "vendor", "reason": "Vendor reply",
    })
    _deadline(db, plan.id, top)
    assert _start(db, plan.id, lower)["admission"]["condition"] == "running"
    assert service._node_row(db, top.id)["order_key"] < service._node_row(db, lower.id)["order_key"]


def test_dependency_does_not_prevent_lower_start(db):
    plan = owner_plan(db)
    top = task(db, plan.id, "Wait for prerequisite")
    lower = task(db, plan.id, "Lower work")
    prerequisite = task(db, plan.id, "Prerequisite", later=True)
    service.add_dependency(top.id, prerequisite.id, db=db)
    db.commit()
    assert _start(db, plan.id, lower)["admission"]["condition"] == "running"


def test_artifact_watch_does_not_prevent_lower_start(db):
    plan = owner_plan(db)
    top = task(db, plan.id, "Wait for PR")
    lower = task(db, plan.id, "Lower work")
    queue_active(db, top.id)
    plan_update(db, plan.id, "arm_watch", {
        "node_id": top.id, "expected_revision": revision(db, top.id),
        "condition": {"type": "event", "value": "ci.complete"},
        "artifact_id": _artifact(db, external_id="owner/repo#1", owner="owner"),
        "expected_event": "github.check_suite.completed",
    })
    from plans import admission_projection
    cause = admission_projection.causes_many([(service._node_row(db, top.id), service._plan_row(db, plan.id))], db=db)[top.id][0]
    assert cause["artifact_id"] is not None
    assert _start(db, plan.id, lower)["admission"]["condition"] == "running"


def test_reorder_above_checkback_allows_start(db):
    plan = owner_plan(db)
    top = task(db, plan.id, "Top check-back")
    lower = task(db, plan.id, "Lower work")
    _deadline(db, plan.id, top)
    plan_update(db, plan.id, "reorder", {
        "node_id": lower.id, "expected_revision": revision(db, lower.id), "order_key": 0,
    })
    assert _start(db, plan.id, lower)["admission"]["condition"] == "running"


@pytest.mark.parametrize("other_actor", ["conductor", "parent"])
def test_other_thread_is_exempt_from_order_refusal(db, other_actor):
    from plans import triage_lines

    plan = owner_plan(db)
    top = task(db, plan.id, "Top check-back")
    lower = task(db, plan.id, "Lower work")
    _deadline(db, plan.id, top)
    assert triage_lines.out_of_order(db, service._node_row(db, lower.id), actor_id=other_actor) is None


def test_mixed_causes_under_phase_do_not_prevent_lower_start(db):
    plan = owner_plan(db)
    phase = service.add_node(plan.id, kind="phase", title="Mixed waits", acceptance_criteria="Done", db=db)
    checkback = task(db, plan.id, "Check-back child", parent_id=phase.id)
    blocked = task(db, plan.id, "Vendor child", parent_id=phase.id)
    lower = task(db, plan.id, "Lower work")
    _deadline(db, plan.id, checkback)
    plan_update(db, plan.id, "block_node", {
        "node_id": blocked.id, "expected_revision": revision(db, blocked.id),
        "owner_kind": "external", "reason_key": "vendor", "reason": "Vendor reply",
    })
    assert _start(db, plan.id, lower)["admission"]["condition"] == "running"


def test_paused_checkback_does_not_prevent_lower_start(db):
    plan = owner_plan(db)
    top = task(db, plan.id, "Paused check-back")
    lower = task(db, plan.id, "Lower work")
    _deadline(db, plan.id, top)
    plan_update(db, plan.id, "pause_node", {
        "node_id": top.id, "expected_revision": revision(db, top.id), "reason": "Paused by person",
    }, actor=HUMAN)
    assert _start(db, plan.id, lower)["admission"]["condition"] == "running"


def test_person_can_start_below_checkback(db):
    plan = owner_plan(db)
    top = task(db, plan.id, "Top check-back")
    lower = task(db, plan.id, "Lower work")
    _deadline(db, plan.id, top)
    assert _start(db, plan.id, lower, actor=HUMAN)["admission"]["condition"] == "running"


def _phase_with_step(db, plan_id):
    phase = service.add_node(
        plan_id, kind="phase", title="Running phase", acceptance_criteria="Phase is verified", db=db,
    )
    step = task(db, plan_id, "Step in the phase", parent_id=phase.id)
    return phase, step


@pytest.mark.parametrize("above", ["set_aside", "not_started"])
def test_a_step_inside_a_lower_phase_waits_for_the_row_above_until_it_is_moved(db, above):
    """A step under a top-level phase starts only once the top rows above it are done or moved down."""
    plan = owner_plan(db)
    top = task(db, plan.id, "Top row")
    phase, step = _phase_with_step(db, plan.id)
    if above == "set_aside":
        plan_update(db, plan.id, "arm_watch", {
            "node_id": top.id, "expected_revision": revision(db, top.id),
            "condition": {"type": "deadline",
                          "value": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()},
            "reason": "Revisit the plan",
        })

    with pytest.raises(service.PlanError, match="Top row"):
        _start(db, plan.id, step)

    plan_update(db, plan.id, "reorder", {
        "node_id": top.id, "expected_revision": revision(db, top.id),
        "order_key": service._node_row(db, phase.id)["order_key"],
    })
    assert service._node_row(db, top.id)["order_key"] > service._node_row(db, phase.id)["order_key"]
    assert _start(db, plan.id, step)["admission"]["condition"] == "running"
