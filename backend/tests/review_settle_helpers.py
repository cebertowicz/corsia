"""A workflow run with a review step, driven through the real plan boundary and harvest."""

from __future__ import annotations

import json
import uuid

from plans import boundary, delegation, mutations, service, workflow_fields
from plans.boundary import PlanActor

AUTHOR = "parent"
TWO = [
    {"id": "build", "required": True, "difficulty": "routine"},
    {"id": "review", "by": "reviewer", "required": True, "after": ["build"], "difficulty": "routine"},
]
THREE = [*TWO, {"id": "ship", "after": ["review"], "difficulty": "routine"}]
ACCEPT = {"verification_result": "passed", "evidence": "Built and checked"}
FINDINGS = "1. The totals are wrong for refunds (worst).\n2. The heading is misspelled."


def thread(db, thread_id: str, *, user="user-1", project="project-1", spawned_by=AUTHOR, model="native/model"):
    db.execute(
        "INSERT OR IGNORE INTO threads (id,user_id,project_id,spawned_by,model,created_at,updated_at)"
        " VALUES (?,?,?,?,?,'t','t')", (thread_id, user, project, spawned_by, model),
    )


def run(db, steps=TWO):
    """The author's plan, the run's phase, and its steps by id."""
    plan = service.ensure_plan("thread", AUTHOR, db=db)
    phase = service.add_node(
        plan.id, kind="phase", title="Run", acceptance_criteria="Done", intent="Ship it",
        workflow_ref="review-run", workflow_version="v1", db=db,
    )
    listed = workflow_fields.build_run(db, phase, steps, causing_executor=AUTHOR)
    return plan, phase.id, {entry["title"]: entry["id"] for entry in listed}


def act(db, plan_id, operation, node_id, *, as_thread=AUTHOR, user=False, **params):
    """One plan_update through the real boundary; its result, or the refusal text."""
    actor = PlanActor(None, "user-1", None, kind="user") if user else PlanActor(as_thread, "user-1", "project-1")
    body = dict(params)
    if node_id is not None:
        body = {"node_id": node_id, "expected_revision": service._node_row(db, node_id)["revision"], **params}
    try:
        return boundary.mutate(
            plan_id, operation, body, actor=actor, provenance="test", mutation_id=uuid.uuid4().hex, db=db,
        ).result
    except service.PlanError as error:
        return str(error)


def hand(db, step: str, worker: str) -> None:
    """The author hands ``step`` to ``worker``, a thread it started."""
    thread(db, worker)
    if service._node_row(db, step)["state"] == "pending":
        service.transition_node(step, "active", db=db)
    if service._node_row(db, step)["spawned_thread_id"] != worker:
        mutations.attach_spawned_thread(step, worker, db=db)


def work(db, worker: str) -> tuple[str, str]:
    """The worker's plan and a started task in it."""
    child = service.ensure_plan("thread", worker, db=db)
    node = service.add_node(child.id, kind="task", title=f"Work {uuid.uuid4().hex[:6]}", acceptance_criteria="Done", db=db)
    service.transition_node(node.id, "active", actor_id=worker, db=db)
    return child.id, node.id


def submit(db, worker: str, *, verdict=None, evidence=FINDINGS):
    """The worker submits its task through the real request_review dispatch; the result or refusal."""
    plan_id, node_id = work(db, worker)
    extra = {} if verdict is None else {"verdict": verdict}
    return act(db, plan_id, "request_review", node_id, as_thread=worker, evidence=evidence, **extra), node_id


def harvest(db) -> list[str]:
    return delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[])


def build_done(db, plan_id: str, build: str) -> None:
    hand(db, build, "builder")
    submit(db, "builder")
    assert build in harvest(db)
    assert act(db, plan_id, "accept", build, **ACCEPT)["node"]["state"] == "accepted"


def wakes(db, kind: str, recipient: str = AUTHOR) -> list[str]:
    return [row["content"] for row in db.execute(
        "SELECT content FROM messages WHERE thread_id=? AND id LIKE ? ORDER BY rowid", (recipient, f"plan-wake:{kind}:%"),
    )]


def ended(db, phase: str) -> list[dict]:
    return [json.loads(row["payload_json"]) for row in db.execute(
        "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='workflow_run_ended'", (phase,),
    )]


def revision_of(db, step: str):
    return db.execute("SELECT * FROM plan_nodes WHERE follow_up_of=?", (step,)).fetchall()
