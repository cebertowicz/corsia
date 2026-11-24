"""A delegated task reports motion of its own commitment."""

from plans import activity_projection, admission, service, status, watch_operations
from plans.boundary import PlanActor
from tests.child_parent_notice_helpers import begin_worker_turn, db, delegated

__all__ = ["db"]


def test_watched_commitment_does_not_follow_unrelated_worker_turn(db):
    db.execute("UPDATE threads SET spawned_by=NULL WHERE id='parent'")
    step, child_plan, child = delegated(db)
    service.transition_node(child.id, "active", actor_id="worker", db=db)
    selected, conflict = admission.select(child.id, actor_id="worker", provenance="test", db=db)
    assert selected is not None and conflict is None
    watch_operations.arm(child_plan.id, {
        "node_id": child.id,
        "condition": {"type": "deadline", "value": "2999-01-01T00:00:00+00:00"},
        "expected_event": "test.slot",
        "reason": "Waiting for test slot",
        "expected_revision": service.get_node(child.id, db=db).revision,
    }, PlanActor("worker", "user-1", "project-1"), db)
    begin_worker_turn(db)
    parent = service.ensure_plan("thread", "parent", db=db)
    read = status.read(db, service._plan_row(db, parent.id), actor=PlanActor("parent", "user-1", "project-1"))
    row = next(row for row in read["current"]["rows"] if row["id"] == step.id)
    assert (row["label"], row["condition"]) == ("waiting", "waiting")
    assert row["reason"] == "Waiting for the delegated child to take up its open steps"
    activity = activity_projection.read_project("project-1", actor=PlanActor(None, "user-1", None, kind="user"), db=db)
    parent_activity = next(item for item in activity["plans"] if item["plan"]["id"] == parent.id)
    assert parent_activity["display"]["label_key"] != "working"


def test_one_unparked_matching_row_preserves_working(db):
    step, child_plan, child = delegated(db)
    watch_operations.arm(child_plan.id, {
        "node_id": child.id,
        "condition": {"type": "deadline", "value": "2999-01-01T00:00:00+00:00"},
        "expected_event": "test.slot", "reason": "Waiting for test slot",
        "expected_revision": service.get_node(child.id, db=db).revision,
    }, PlanActor("worker", "user-1", "project-1"), db)
    other = service.add_node(child_plan.id, kind="task", title="Other step",
                             acceptance_criteria="Done", db=db)
    service.transition_node(other.id, "active", actor_id="worker", db=db)
    selected, conflict = admission.select(other.id, actor_id="worker", provenance="test", db=db)
    assert selected is not None and conflict is None
    assert service._node_row(db, other.id)["commitment_id"] == service._node_row(db, step.id)["commitment_id"]
    begin_worker_turn(db)
    parent = service.ensure_plan("thread", "parent", db=db)
    read = status.read(db, service._plan_row(db, parent.id), actor=PlanActor("parent", "user-1", "project-1"))
    row = next(row for row in read["current"]["rows"] if row["id"] == step.id)
    assert (row["label"], row["condition"]) == ("working", "running")


def test_child_waiting_on_dependency_does_not_follow_unrelated_turn(db):
    step, child_plan, child = delegated(db)
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    prerequisite = service.add_node(parent_plan.id, kind="task", title="Prerequisite",
                                    acceptance_criteria="Done", db=db)
    service.add_dependency(child.id, prerequisite.id, db=db)
    begin_worker_turn(db)
    parent = service.ensure_plan("thread", "parent", db=db)
    read = status.read(db, service._plan_row(db, parent.id), actor=PlanActor("parent", "user-1", "project-1"))
    row = next(row for row in read["current"]["rows"] if row["id"] == step.id)
    assert (row["label"], row["condition"]) == ("waiting", "waiting")


def test_legacy_child_without_matching_row_keeps_turn_wide_motion(db):
    step, _plan, _child = delegated(db, with_worker_node=False)
    begin_worker_turn(db)
    parent = service.ensure_plan("thread", "parent", db=db)
    read = status.read(db, service._plan_row(db, parent.id), actor=PlanActor("parent", "user-1", "project-1"))
    row = next(row for row in read["current"]["rows"] if row["id"] == step.id)
    assert (row["label"], row["condition"]) == ("working", "running")


def test_child_working_its_matching_row_still_reads_working(db):
    step, _plan, child = delegated(db)
    service.transition_node(child.id, "active", actor_id="worker", db=db)
    selected, conflict = admission.select(child.id, actor_id="worker", provenance="test", db=db)
    assert selected is not None and conflict is None
    begin_worker_turn(db)
    parent = service.ensure_plan("thread", "parent", db=db)
    read = status.read(db, service._plan_row(db, parent.id), actor=PlanActor("parent", "user-1", "project-1"))
    row = next(row for row in read["current"]["rows"] if row["id"] == step.id)
    assert (row["label"], row["condition"]) == ("working", "running")
