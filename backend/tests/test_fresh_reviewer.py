"""A reviewer step settles only from a worker that has seen none of the work it checks."""

import uuid

import pytest

from plans import boundary, delegation, mutations, service, workflow_fields
from plans.boundary import PlanActor
from plans.fresh_reviewer import AUTHOR_FAIL, NOT_FRESH
from tests.child_parent_notice_helpers import db
from tests.plan_review_helpers import submit_review_evidence

__all__ = ["db"]

STEPS = [
    {"id": "build", "difficulty": "routine"},
    {"id": "review", "by": "reviewer", "required": True, "difficulty": "routine"},
]
ACCEPT = {"verification_result": "passed", "evidence": "Reviewed worker return"}


def _run(db):
    parent = service.ensure_plan("thread", "parent", db=db)
    phase = service.add_node(parent.id, kind="phase", title="Run", acceptance_criteria="Done", intent="Ship it", db=db)
    build, review = (entry["id"] for entry in workflow_fields.build_run(db, phase, STEPS, causing_executor="parent"))
    return parent, build, review


def _return(db, step: str, worker: str) -> None:
    """``worker``, spawned by the plan's owner and attached to ``step``, returns and is harvested."""
    db.execute(
        "INSERT OR IGNORE INTO threads (id,user_id,project_id,spawned_by,model,created_at,updated_at)"
        " VALUES (?,'user-1','project-1','parent','native/model','t','t')", (worker,),
    )
    if service._node_row(db, step)["state"] == "pending":
        service.transition_node(step, "active", db=db)
    if service._node_row(db, step)["spawned_thread_id"] != worker:
        mutations.attach_spawned_thread(step, worker, db=db)
    child = service.ensure_plan("thread", worker, db=db)
    work = service.add_node(child.id, kind="task", title=f"Work {uuid.uuid4().hex[:6]}", acceptance_criteria="Done", db=db)
    service.transition_node(work.id, "active", actor_id=worker, db=db)
    submit_review_evidence(work.id, db=db, actor_id=worker)
    assert step in delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[])


def _act(db, plan_id, operation, step, *, kind="thread", **params):
    actor = PlanActor("parent", "user-1", "project-1") if kind == "thread" else PlanActor(None, "user-1", None, kind="user")
    try:
        return boundary.mutate(
            plan_id, operation, {"node_id": step, "expected_revision": service._node_row(db, step)["revision"], **params},
            actor=actor, provenance="test", mutation_id=uuid.uuid4().hex, db=db,
        ).result
    except service.PlanError as error:
        return str(error)


def test_a_worker_that_built_the_work_cannot_settle_its_review(db):
    parent, build, review = _run(db)
    _return(db, build, "builder")
    _act(db, parent.id, "accept", build, **ACCEPT)
    _return(db, review, "builder")
    assert _act(db, parent.id, "accept", review, **ACCEPT) == NOT_FRESH.format(worker="builder")
    assert service._node_row(db, review)["state"] == "review_requested"


def test_the_refusal_route_leads_to_a_settled_step(db):
    """The refusal names replace_spawned_thread; following it, a new worker's return settles the step."""
    parent, build, review = _run(db)
    _return(db, build, "builder")
    _act(db, parent.id, "accept", build, **ACCEPT)
    _return(db, review, "builder")
    assert "replace_spawned_thread" in _act(db, parent.id, "accept", review, **ACCEPT)
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,spawned_by,model,created_at,updated_at)"
        " VALUES ('fresh','user-1','project-1','parent','native/model','t','t')",
    )
    assert not isinstance(_act(db, parent.id, "replace_spawned_thread", review, spawned_thread_id="fresh"), str)
    _return(db, review, "fresh")
    assert _act(db, parent.id, "accept", review, **ACCEPT)["node"]["state"] == "accepted"


def test_a_fresh_worker_settles_it(db):
    parent, build, review = _run(db)
    _return(db, build, "builder")
    _return(db, review, "reviewer")
    assert _act(db, parent.id, "accept", review, **ACCEPT)["node"]["state"] == "accepted"


def test_the_person_may_settle_a_review_whoever_returned_it(db):
    parent, build, review = _run(db)
    _return(db, build, "builder")
    _act(db, parent.id, "accept", build, **ACCEPT)
    _return(db, review, "builder")
    assert _act(db, parent.id, "accept", review, kind="user", **ACCEPT)["node"]["state"] == "accepted"


def test_the_same_reviewer_takes_the_next_attempt_of_its_step(db):
    parent, build, review = _run(db)
    _return(db, build, "builder")
    _return(db, review, "reviewer")
    revision = _act(db, parent.id, "reject", review, feedback="Check the edge case")["revision"]["node_id"]
    assert service._node_row(db, revision)["spawned_thread_id"] == "reviewer"
    (revise,) = (row["id"] for row in db.execute(
        "SELECT n.id FROM plan_nodes n JOIN plans p ON p.id=n.plan_id WHERE p.owner_id='reviewer' AND n.state='pending'",
    ))
    service.transition_node(revise, "active", actor_id="reviewer", db=db)
    submit_review_evidence(revise, db=db, actor_id="reviewer")
    assert revision in delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[])
    assert _act(db, parent.id, "accept", revision, **ACCEPT)["node"]["state"] == "accepted"


def test_an_agent_cannot_fail_a_review_step_but_the_person_can(db):
    parent, _build, review = _run(db)
    service.transition_node(review, "active", db=db)
    assert _act(db, parent.id, "fail", review, evidence="Review failed") == AUTHOR_FAIL
    assert _act(db, parent.id, "fail", review, kind="user", evidence="Review failed")["node"]["state"] == "failed"


def test_the_author_fail_refusal_route_works_once_the_review_is_back(db):
    """Fail is refused on a returned review too; the route it names, reject with feedback, is taken."""
    parent, build, review = _run(db)
    _return(db, build, "builder")
    _return(db, review, "reviewer")
    assert _act(db, parent.id, "fail", review, evidence="Review failed") == AUTHOR_FAIL
    revision = _act(db, parent.id, "reject", review, feedback="Fixed the edge case; check again")["revision"]["node_id"]
    assert service._node_row(db, revision)["spawned_thread_id"] == "reviewer"


def test_a_nested_runs_review_reaches_the_work_of_the_outer_run(db):
    """A run that is itself a step checks the outer run's work: its builder can't settle the inner review."""
    parent = service.ensure_plan("thread", "parent", db=db)
    outer = service.add_node(parent.id, kind="phase", title="Run", acceptance_criteria="Done", intent="Ship it", db=db)
    listed = workflow_fields.build_run(db, outer, [
        {"id": "build", "difficulty": "routine"},
        {"id": "check", "workflow": "inner", "difficulty": "routine", "steps": [STEPS[1]]},
    ], causing_executor="parent")
    build, review = listed[0]["id"], listed[1]["steps"][0]["id"]
    _return(db, build, "builder")
    _act(db, parent.id, "accept", build, **ACCEPT)
    _return(db, review, "builder")
    assert _act(db, parent.id, "accept", review, **ACCEPT) == NOT_FRESH.format(worker="builder")


def test_a_worker_on_a_build_steps_sub_task_cannot_settle_the_review(db):
    """A sub-task under a build step is part of the run's work, so its worker has seen it."""
    parent, build, review = _run(db)
    service.transition_node(build, "active", db=db)
    helper = service.add_node(parent.id, kind="task", title="Gather figures", acceptance_criteria="Done", parent_id=build, db=db)
    _return(db, helper.id, "helper")
    _act(db, parent.id, "accept", helper.id, **ACCEPT)
    _return(db, review, "helper")
    assert _act(db, parent.id, "accept", review, **ACCEPT) == NOT_FRESH.format(worker="helper")


@pytest.mark.parametrize("operation", ["accept", "fail"])
def test_other_steps_are_untouched(db, operation):
    parent, build, review = _run(db)
    _return(db, review, "reviewer")
    _act(db, parent.id, "accept", review, **ACCEPT)
    _return(db, build, "reviewer")
    params = ACCEPT if operation == "accept" else {"evidence": "Did not build"}
    assert not isinstance(_act(db, parent.id, operation, build, **params), str)


def test_a_required_review_steps_skip_refusal_offers_no_fail(db):
    """The skip refusal names the routes that settle a review step, not fail, which is refused there."""
    parent = service.ensure_plan("thread", "parent", db=db)
    phase = service.add_node(parent.id, kind="phase", title="Run", acceptance_criteria="Done", intent="Ship it", db=db)
    build, review = (entry["id"] for entry in workflow_fields.build_run(
        db, phase, [{**STEPS[0], "required": True}, STEPS[1]], causing_executor="parent"))
    service.transition_node(review, "active", db=db)
    refusal = _act(db, parent.id, "close_node", review, reason="Not needed")
    assert refusal == workflow_fields.REQUIRED_REVIEW_STEP_SKIP.format(step="review", route=AUTHOR_FAIL)
    assert "or fail it" not in refusal
    # The person may fail a review step, so archiving it they are told so, not given an agent's routes.
    from plans import archival

    with pytest.raises(service.PlanError) as person:
        archival.archive_node(review, actor_id="user:user-1", db=db)
    assert str(person.value) == workflow_fields.REQUIRED_STEP_SKIP.format(step="review")
    service.transition_node(build, "active", db=db)
    assert _act(db, parent.id, "close_node", build, reason="Not needed") == workflow_fields.REQUIRED_STEP_SKIP.format(step="build")


def test_archiving_a_delegated_review_step_offers_no_fail(db):
    """An optional review step still with its reviewer is archived after its verdict, never by failing it."""
    from plans import archival

    parent = service.ensure_plan("thread", "parent", db=db)
    phase = service.add_node(parent.id, kind="phase", title="Run", acceptance_criteria="Done", intent="Ship it", db=db)
    review = workflow_fields.build_run(
        db, phase, [{"id": "review", "by": "reviewer", "difficulty": "routine"}], causing_executor="parent",
    )[0]["id"]
    service.transition_node(review, "active", db=db)
    mutations.attach_spawned_thread(review, "worker", db=db)
    with pytest.raises(service.PlanError) as live:
        archival.archive_node(review, actor_id="parent", db=db)
    assert str(live.value) == archival._REVIEW_DELEGATION_REFUSED
    db.execute("UPDATE threads SET archived_at='t' WHERE id='worker'")
    with pytest.raises(service.PlanError) as gone:
        archival.archive_node(review, actor_id="parent", db=db)
    assert str(gone.value) == archival._REVIEW_DELEGATION_REFUSED_UNREACHABLE
    assert "fail" not in str(live.value) + str(gone.value)
    # The person may fail it, so they keep the route that says so.
    with pytest.raises(service.PlanError) as person:
        archival.archive_node(review, actor_id="user:user-1", db=db)
    assert str(person.value) == archival._DELEGATION_REFUSED_UNREACHABLE


def test_a_reviewers_failed_step_wakes_the_parent_without_offering_fail(db):
    """A review step's worker stops with a failed step: the wake routes to a new reviewer, not to fail."""
    from plans.fresh_reviewer import REVIEW_STEP_CHILD_FAILED_TEMPLATE
    from tests.child_parent_notice_helpers import begin_worker_turn, end_worker_turn, other_open_step, wakes

    parent, _build, review = _run(db)
    service.transition_node(review, "active", db=db)
    mutations.attach_spawned_thread(review, "worker", db=db)
    plan = service.ensure_plan("thread", "worker", db=db)
    node = service.add_node(plan.id, kind="task", title="Check", acceptance_criteria="Done", db=db)
    other_open_step(db, plan)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    begin_worker_turn(db)
    mutations.fail_node(node.id, actor_id="worker", db=db)
    end_worker_turn(db)

    content = str(wakes(db)[0]["content"]).splitlines()[0]
    assert content == REVIEW_STEP_CHILD_FAILED_TEMPLATE.format(
        title=service._node_row(db, review)["title"])
    assert "fail the delegated step" not in content
