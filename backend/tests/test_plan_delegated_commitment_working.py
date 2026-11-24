"""A delegated task reports motion of its own commitment."""

from plans import activity_projection, admission, decisions, holds, mutations, service, status, watch_operations
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


def _step_row(db, step):
    parent = service.ensure_plan("thread", "parent", db=db)
    read = status.read(db, service._plan_row(db, parent.id), actor=PlanActor("parent", "user-1", "project-1"))
    return next(row for row in read["current"]["rows"] if row["id"] == step.id)


def test_row_sequenced_after_the_helpers_running_row_keeps_working(db):
    """A row the helper started before it was attached keeps its own commitment;
    the step's row that waits behind it is that work, not parked."""
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    step = service.add_node(parent_plan.id, kind="task", title="Delegate work",
                            acceptance_criteria="Reviewed", spawn_kind="claude-code", db=db)
    service.transition_node(step.id, "active", db=db)
    worker_plan = service.ensure_plan("thread", "worker", db=db)
    started = service.add_node(worker_plan.id, kind="task", title="Started before attach",
                               acceptance_criteria="Done", db=db)
    service.transition_node(started.id, "active", actor_id="worker", db=db)
    selected, conflict = admission.select(started.id, actor_id="worker", provenance="test", db=db)
    assert selected is not None and conflict is None
    mutations.attach_spawned_thread(step.id, "worker", db=db)
    follow = service.add_node(worker_plan.id, kind="task", title="Follow-up",
                              acceptance_criteria="Done", db=db)
    step_commitment = service._node_row(db, step.id)["commitment_id"]
    assert service._node_row(db, started.id)["commitment_id"] != step_commitment
    assert service._node_row(db, follow.id)["commitment_id"] == step_commitment
    service.add_dependency(follow.id, started.id, db=db)
    begin_worker_turn(db)
    row = _step_row(db, step)
    assert (row["label"], row["condition"]) == ("working", "running")


def test_row_waiting_on_a_raised_decision_does_not_read_working(db):
    step, child_plan, child = delegated(db)
    worker, parent = PlanActor("worker", "user-1", "project-1"), PlanActor("parent", "user-1", "project-1")
    drafted = decisions.draft(
        child_plan.id, source_node_id=child.id, mutation_id="decide-1",
        actor=worker, provenance="tool.plans", db=db,
        title="Pick the storage shape", context="Two shapes fit.",
        tradeoff="Rows are simpler; columns are faster.",
        recommendation="A", rationale="Simpler wins here.",
        options=[{"id": "A", "label": "Rows"}, {"id": "B", "label": "Columns"}],
        other_enabled=True,
    ).result["decision"]
    reviewed = decisions.parent_review(
        drafted["id"], expected_revision=drafted["revision"], mutation_id="review-1",
        actor=parent, provenance="tool.plans", db=db,
    ).result["decision"]
    decisions.raise_to_user(
        reviewed["id"], expected_revision=reviewed["revision"], mutation_id="raise-1",
        actor=parent, provenance="tool.plans", db=db,
    )
    begin_worker_turn(db)
    row = _step_row(db, step)
    assert (row["label"], row["condition"]) == ("waiting", "waiting")


def test_held_row_keeps_the_step_waiting_on_its_helper(db):
    """A placed hold leaves no movable row; the step still waits on its busy
    helper rather than turning into the parent's own ready work."""
    step, _plan, child = delegated(db)
    holds.place_hold(child.id, kind="blocked", reason_key="test_hold", reason="Hold",
                     owner_kind="thread", owner_id="worker",
                     expected_revision=service.get_node(child.id, db=db).revision,
                     actor_id="worker", provenance="test", db=db)
    begin_worker_turn(db)
    row = _step_row(db, step)
    assert (row["label"], row["condition"]) == ("waiting", "waiting")
    assert row["reason"] == "Waiting for the delegated child to take up its open steps"
