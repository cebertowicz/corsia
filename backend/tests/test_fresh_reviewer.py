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


@pytest.mark.parametrize("operation", ["accept", "fail"])
def test_other_steps_are_untouched(db, operation):
    parent, build, review = _run(db)
    _return(db, review, "reviewer")
    _act(db, parent.id, "accept", review, **ACCEPT)
    _return(db, build, "reviewer")
    params = ACCEPT if operation == "accept" else {"evidence": "Did not build"}
    assert not isinstance(_act(db, parent.id, operation, build, **params), str)
