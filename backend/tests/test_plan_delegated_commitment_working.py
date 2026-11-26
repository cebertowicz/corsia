"""A delegated task reports motion of its own commitment."""

from plans import activity_projection, admission, decisions, holds, mutations, service, status, watch_operations
from plans.boundary import PlanActor
from tests.child_parent_notice_helpers import begin_worker_turn, db, delegated, other_open_step

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


def test_compact_read_parks_a_row_behind_a_running_row_and_an_idle_helper(db):
    """Activity's compact read parks from the same causes as the full read. The
    step's row waits on the helper's running row and on its own idle helper, so
    it is parked in both reads and neither reads the step as Working."""
    db.execute("UPDATE threads SET spawned_by=NULL WHERE id='parent'")
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    step = service.add_node(parent_plan.id, kind="task", title="Delegate work",
                            acceptance_criteria="Reviewed", spawn_kind="claude-code", db=db)
    service.transition_node(step.id, "active", db=db)
    child_plan = service.ensure_plan("thread", "worker", db=db)
    running = service.add_node(child_plan.id, kind="task", title="Started before attach",
                               acceptance_criteria="Done", db=db)
    service.transition_node(running.id, "active", actor_id="worker", db=db)
    selected, conflict = admission.select(running.id, actor_id="worker", provenance="test", db=db)
    assert selected is not None and conflict is None
    mutations.attach_spawned_thread(step.id, "worker", db=db)
    child = service.add_node(child_plan.id, kind="task", title="Follow-up", acceptance_criteria="Done",
                             spawn_kind="claude-code", db=db)
    assert service._node_row(db, child.id)["commitment_id"] == service._node_row(db, step.id)["commitment_id"]
    service.add_dependency(child.id, running.id, db=db)
    service.transition_node(child.id, "active", actor_id="worker", db=db)
    db.execute("INSERT INTO threads (id,user_id,project_id,spawned_by,model,created_at,updated_at) "
               "VALUES ('helper','user-1','project-1','worker','native/model','t','t')")
    mutations.attach_spawned_thread(child.id, "helper", db=db)
    helper_plan = service.ensure_plan("thread", "helper", db=db)
    service.add_node(helper_plan.id, kind="task", title="Helper step", acceptance_criteria="Done", db=db)
    begin_worker_turn(db)
    row = _step_row(db, step)
    assert (row["label"], row["condition"]) == ("waiting", "waiting")
    parent = service.ensure_plan("thread", "parent", db=db)
    activity = activity_projection.read_project("project-1", actor=PlanActor(None, "user-1", None, kind="user"), db=db)
    parent_activity = next(item for item in activity["plans"] if item["plan"]["id"] == parent.id)
    assert parent_activity["display"]["label_key"] != "working"


def _set_aside(db, plan, node, *, hours, why):
    """The worker sets its own step aside through the real boundary (arm_watch with a reason)."""
    from datetime import datetime, timedelta, timezone

    from tests.plan_close_next_helpers import plan_update

    until = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(hours=hours)
    plan_update(db, plan.id, "arm_watch", {
        "node_id": node.id, "expected_revision": service.get_node(node.id, db=db).revision,
        "condition": {"type": "deadline", "value": until.isoformat()}, "reason": why,
    }, actor=PlanActor("worker", "user-1", "project-1"))
    return f"Until {until:%Y-%m-%dT%H:%MZ} · {why}"


def _person_row(db, step):
    from plans import operator_projection

    read = operator_projection.read_thread("parent", actor=PlanActor(None, "user-1", None, kind="user"), db=db)
    return next(node for plan in read["plans"] for node in plan["nodes"] if node["id"] == step.id)


def test_parked_worker_row_says_until_when_and_why(db):
    """A worker that set its step aside reads as set aside, with its time and reason, on the step it holds."""
    step, child_plan, child = delegated(db)
    service.transition_node(child.id, "active", actor_id="worker", db=db)
    shown = _set_aside(db, child_plan, child, hours=8, why="Parked for the thumbs map")
    row = _step_row(db, step)
    assert (row["label"], row["condition"]) == ("waiting", "waiting")
    # The agent's row is cut to a few dozen characters; the person's row carries it whole.
    assert f"Worker set aside · {shown}".startswith(row["reason"].removesuffix("…"))
    assert _person_row(db, step)["status_reason"] == f"Worker set aside · {shown}"


def test_parked_worker_row_names_the_step_that_comes_back_first(db):
    step, child_plan, child = delegated(db)
    later = other_open_step(db, child_plan, title="Second")
    service.transition_node(child.id, "active", actor_id="worker", db=db)
    _set_aside(db, child_plan, later, hours=30, why="After the review")
    first = _set_aside(db, child_plan, child, hours=4, why="After the map")
    assert _person_row(db, step)["status_reason"] == f"Worker set aside · {first}"


def test_worker_with_a_step_not_set_aside_keeps_the_open_steps_wait(db):
    """Set-aside names the wait only when every open step of the worker is set aside."""
    step, child_plan, child = delegated(db)
    other_open_step(db, child_plan, title="Second")
    service.transition_node(child.id, "active", actor_id="worker", db=db)
    _set_aside(db, child_plan, child, hours=4, why="After the map")
    assert _person_row(db, step)["status_reason"] == "Waiting for the delegated child to take up its open steps"
