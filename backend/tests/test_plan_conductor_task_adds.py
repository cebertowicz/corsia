"""Conductors may append tasks to a lane plan and hear their terminal outcome (#488)."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid

import pytest

from infra.schema import SCHEMA_SQL
from plans import archival, closure, service
from plans.boundary import PlanActor, mutate, read_current_plan
from plans.errors import PlanRefusal
from prompts.plan_tools import FOREIGN_PLAN_MUTATION


PROJECT_CONDUCTOR = PlanActor("project-conductor", "user", "project-a")
WORKSPACE_CONDUCTOR = PlanActor("workspace-conductor", "user", None)
PLAIN_AGENT = PlanActor("plain-agent", "user", "project-a")
OTHER_CONDUCTOR = PlanActor("other-conductor", "user", "project-b")
LANE = PlanActor("lane", "user", "project-a")


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    for project in ("project-a", "project-b"):
        connection.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)",
            (project, project, "t", "t"),
        )
    for thread_id, project_id in (
        ("project-conductor", "project-a"),
        ("workspace-conductor", None),
        ("plain-agent", "project-a"),
        ("other-conductor", "project-b"),
        ("lane", "project-a"),
        ("other-lane", "project-b"),
    ):
        connection.execute(
            """INSERT INTO threads (id,user_id,project_id,title,model,created_at,updated_at)
               VALUES (?, 'user', ?, ?, 'test/model', 't', 't')""",
            (thread_id, project_id, thread_id),
        )
    connection.execute(
        "UPDATE projects SET conductor_thread_id='project-conductor' WHERE id='project-a'"
    )
    connection.execute(
        "UPDATE projects SET conductor_thread_id='other-conductor' WHERE id='project-b'"
    )
    connection.execute(
        "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)",
        ("workspace_conductor_session_id", json.dumps({"session_id": "workspace-conductor"}), "t"),
    )
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


def _add(db, plan_id: str, actor: PlanActor, **params):
    return mutate(
        plan_id, "add", {"kind": "task", "title": "Refresh lane context",
        "acceptance_criteria": "Current", **params}, actor=actor,
        provenance="test.conductor_add", mutation_id=uuid.uuid4().hex, db=db,
    ).result["node"]


def _node_created(db, node_id: str):
    row = db.execute(
        "SELECT actor_id,provenance FROM plan_node_events WHERE node_id=? AND event='node_created'",
        (node_id,),
    ).fetchone()
    return None if row is None else (row["actor_id"], row["provenance"])


def _mutate(db, plan_id: str, operation: str, params: dict, actor=LANE):
    return mutate(
        plan_id, operation, params, actor=actor, provenance="test.settle",
        mutation_id=uuid.uuid4().hex, db=db,
    ).result


def test_project_conductor_appends_a_pending_top_level_task_without_waking_lane(db):
    plan = service.ensure_plan("thread", "lane", db=db)
    existing = service.add_node(plan.id, kind="task", title="Existing", acceptance_criteria="Done", db=db)
    db.commit()

    node = _add(db, plan.id, PROJECT_CONDUCTOR, description="Read the updated context")

    assert (node["state"], node["parent_id"], node["executor_thread_id"]) == ("pending", None, None)
    assert node["order_key"] > existing.order_key
    assert _node_created(db, node["id"]) == ("project-conductor", "plan.conductor_add")
    assert db.execute("SELECT COUNT(*) FROM messages WHERE thread_id='lane'").fetchone()[0] == 0


_ROUTES = PlanRefusal, "^" + re.escape(FOREIGN_PLAN_MUTATION) + "$"
_OUT_OF_SCOPE = PermissionError, "^plan is outside the current project scope$"


@pytest.mark.parametrize(
    "actor,operation,params,refusal",
    [
        (PLAIN_AGENT, "add", {}, _ROUTES),
        # Another project's conductor cannot even see the plan.
        (OTHER_CONDUCTOR, "add", {}, _OUT_OF_SCOPE),
        (PROJECT_CONDUCTOR, "insert", {}, _ROUTES),
        (PROJECT_CONDUCTOR, "add", {"parent_id": "placeholder"}, _ROUTES),
        (PROJECT_CONDUCTOR, "add", {"depends_on_node_id": "placeholder"}, _ROUTES),
    ],
)
def test_only_a_conductor_may_append_a_top_level_task_to_a_foreign_plan(
    db, actor, operation, params, refusal,
):
    plan = service.ensure_plan("thread", "lane", db=db)
    parent = service.add_node(plan.id, kind="task", title="Parent", acceptance_criteria="Done", db=db)
    db.commit()
    params = {key: parent.id if value == "placeholder" else value for key, value in params.items()}
    values = {"kind": "task", "title": "No", "acceptance_criteria": "Never", **params}

    kind, message = refusal
    with pytest.raises(kind, match=message):
        mutate(plan.id, operation, values, actor=actor, provenance="test", mutation_id=uuid.uuid4().hex, db=db)


def test_a_conductor_add_to_a_plan_that_does_not_exist_says_so(db):
    with pytest.raises(Exception, match="must create one first"):
        _add(db, "no-such-plan", PROJECT_CONDUCTOR)


def test_a_project_conductor_cannot_read_another_projects_plan(db):
    service.ensure_plan("thread", "other-lane", db=db)
    db.commit()

    with pytest.raises(PermissionError, match="project scope"):
        read_current_plan(actor=PROJECT_CONDUCTOR, thread_id="other-lane", db=db)


def test_the_lane_starting_a_conductor_add_receives_its_description(db):
    plan = service.ensure_plan("thread", "lane", db=db)
    own = service.add_node(plan.id, kind="task", title="Own work", acceptance_criteria="Done", db=db)
    db.commit()
    own_active = _mutate(db, plan.id, "activate", {"node_id": own.id, "expected_revision": own.revision})["node"]
    added = _add(db, plan.id, PROJECT_CONDUCTOR, description="Read the updated context first")

    result = _mutate(db, plan.id, "close_node", {
        "node_id": own.id, "expected_revision": own_active["revision"],
        "reason": "Superseded", "next_node_id": added["id"],
    })

    assert result["selected_description"] == {
        "node_id": added["id"], "text": "Read the updated context first", "cut": False,
    }


def test_workspace_conductor_reads_and_adds_across_projects(db):
    plan = service.ensure_plan("thread", "other-lane", db=db)
    db.commit()

    node = _add(db, plan.id, WORKSPACE_CONDUCTOR)

    assert read_current_plan(actor=WORKSPACE_CONDUCTOR, thread_id="other-lane", db=db)["plan"]["id"] == plan.id
    assert _node_created(db, node["id"]) == ("workspace-conductor", "plan.conductor_add")


@pytest.mark.parametrize("operation", ["accept", "fail", "close_node", "archive_node"])
def test_a_conductor_add_wakes_its_creator_once_when_it_settles(db, operation):
    # The conductor has nothing of its own to do: the wake is news it asked
    # for, not a nudge toward open work.
    lane_plan = service.ensure_plan("thread", "lane", db=db)
    db.commit()
    node = _add(db, lane_plan.id, PROJECT_CONDUCTOR)
    if operation == "accept":
        active = _mutate(db, lane_plan.id, "activate", {
            "node_id": node["id"], "expected_revision": node["revision"],
        })["node"]
        submitted = _mutate(db, lane_plan.id, "request_review", {
            "node_id": node["id"], "evidence": "Verified", "expected_revision": active["revision"],
        })["node"]
        _mutate(db, lane_plan.id, "accept", {
            "node_id": node["id"], "verification_result": "passed",
            "expected_revision": submitted["revision"],
        })
    elif operation == "fail":
        service.transition_node(node["id"], "failed", actor_id="lane", provenance="test", db=db)
    elif operation == "close_node":
        closure.close_node(node["id"], reason="No longer needed", actor_id="lane", provenance="test", db=db)
    else:
        archival.archive_node(node["id"], actor_id="lane", provenance="test", db=db)
    db.commit()

    wakes = db.execute(
        "SELECT content FROM messages WHERE thread_id='project-conductor' AND id LIKE 'plan-wake:conductor_task_settled:%'"
    ).fetchall()
    assert len(wakes) == 1
    assert "Refresh lane context" in wakes[0]["content"]
    assert {"accept": "is done", "fail": "failed", "close_node": "was closed",
            "archive_node": "was archived"}[operation] in wakes[0]["content"]


def test_archiving_a_settled_conductor_add_does_not_wake_again(db):
    service.ensure_plan("thread", "project-conductor", db=db)
    lane_plan = service.ensure_plan("thread", "lane", db=db)
    db.commit()
    node = _add(db, lane_plan.id, PROJECT_CONDUCTOR)
    closure.close_node(node["id"], reason="No longer needed", actor_id="lane", provenance="test", db=db)
    archival.archive_node(node["id"], actor_id="lane", provenance="test", db=db)
    db.commit()

    wakes = db.execute(
        "SELECT content FROM messages WHERE thread_id='project-conductor' AND id LIKE 'plan-wake:conductor_task_settled:%'"
    ).fetchall()
    assert len(wakes) == 1
    assert "was closed: No longer needed" in wakes[0]["content"]


def test_a_sent_back_conductor_add_wakes_its_creator_once_when_the_revision_settles(db):
    lane_plan = service.ensure_plan("thread", "lane", db=db)
    db.commit()
    node = _add(db, lane_plan.id, PROJECT_CONDUCTOR)
    active = _mutate(db, lane_plan.id, "activate", {
        "node_id": node["id"], "expected_revision": node["revision"],
    })["node"]
    submitted = _mutate(db, lane_plan.id, "request_review", {
        "node_id": node["id"], "evidence": "First try", "expected_revision": active["revision"],
    })["node"]
    revision = _mutate(db, lane_plan.id, "reject", {
        "node_id": node["id"], "feedback": "Missing the skills index",
        "expected_revision": submitted["revision"],
    })["revision"]
    db.commit()
    wakes = "SELECT content FROM messages WHERE thread_id='project-conductor' AND id LIKE 'plan-wake:conductor_task_settled:%'"
    # A send-back is not an outcome.
    assert db.execute(wakes).fetchall() == []

    active = _mutate(db, lane_plan.id, "activate", {
        "node_id": revision["node_id"], "expected_revision": revision["revision"],
    })["node"]
    submitted = _mutate(db, lane_plan.id, "request_review", {
        "node_id": revision["node_id"], "evidence": "Index added", "expected_revision": active["revision"],
    })["node"]
    _mutate(db, lane_plan.id, "accept", {
        "node_id": revision["node_id"], "verification_result": "passed",
        "expected_revision": submitted["revision"],
    })
    db.commit()

    # Archiving the sent-back original afterwards is not a second outcome.
    archival.archive_node(node["id"], actor_id="lane", provenance="test", db=db)
    db.commit()

    settled = db.execute(wakes).fetchall()
    assert len(settled) == 1
    assert "is done" in settled[0]["content"]


def test_send_back_seeded_foreign_actor_does_not_wake_a_conductor(db):
    conductor_plan = service.ensure_plan("thread", "project-conductor", db=db)
    service.add_node(conductor_plan.id, kind="task", title="Conductor follow-up", acceptance_criteria="Done", db=db)
    lane_plan = service.ensure_plan("thread", "lane", db=db)
    node = service.add_node(
        lane_plan.id, kind="task", title="Send-back worker step", acceptance_criteria="Done",
        actor_id="project-conductor", provenance="plans.rejection", db=db,
    )
    service.transition_node(node.id, "failed", actor_id="lane", provenance="test", db=db)
    db.commit()

    assert db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id='project-conductor' AND id LIKE 'plan-wake:conductor_task_settled:%'"
    ).fetchone()[0] == 0
