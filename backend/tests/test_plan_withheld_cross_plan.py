"""Foreign dependency and worker results stay behind their current project fence."""

import json

from infra.project_fence import WITHHELD_LOCAL_ONLY
from plans import cross_plan, plan_wake, service
from plans.current import read_current_status
from tests.test_plan_cross_plan_dependency import LANE, _setup, _wakes, db  # noqa: F401


def _fence(db):
    db.execute("INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES ('corsia_agent_thread_id',?,'t')",
               (json.dumps({"session_id": "lane"}),))
    db.execute("INSERT INTO project_inference_policies(project_id,cloud_models_off) VALUES ('elsewhere',1)")
    db.execute("UPDATE threads SET project_id='elsewhere' WHERE id='infra'")
    db.commit()


def test_own_plan_hides_a_now_fenced_prerequisite(db):
    _infra, fix, _lane, task = _setup(db)
    _fence(db)
    detail = read_current_status(actor=LANE, node_id=task.id, db=db)
    status = read_current_status(actor=LANE, db=db)
    assert "Fix the dev test" not in json.dumps(detail)
    assert "Fix the dev test" not in json.dumps(status)
    cause = next(c for c in detail["execution"]["causes"] if c.get("dependency_node_id") == fix.id)
    assert cause["reason"] == WITHHELD_LOCAL_ONLY


def test_foreign_prerequisite_wake_hides_title(db):
    _infra, fix, lane, task = _setup(db)
    _fence(db)
    prerequisite = cross_plan._prerequisite(db, fix.id)
    dependent = {"id": task.id, "title": "Merge my PR", "plan_id": lane.id, "owner_id": "lane"}
    cross_plan._notify(db, recipient="lane", plan_id=lane.id, dependent=dependent,
                       prerequisite=prerequisite, owner="Infrastructure", outcome="done")
    content = _wakes(db)[-1]["content"]
    assert "Fix the dev test" not in content
    assert content.startswith(WITHHELD_LOCAL_ONLY)
    assert fix.id in content


def test_plan_wake_writer_projects_foreign_source(db):
    infra, fix, _lane, _task = _setup(db)
    _fence(db)
    content = 'Secret wake\n' + json.dumps({"node_id": fix.id, "title": "Secret title", "count": 1})
    accepted = plan_wake.append_input(db, recipient="lane", message_id="test-foreign-wake", content=content,
                                      metadata={"event_type": "plan.task_closed", "plan_id": infra.id})
    assert accepted is not None
    stored = db.execute("SELECT content FROM messages WHERE id='test-foreign-wake'").fetchone()[0]
    assert "Secret" not in stored
    assert stored.startswith(WITHHELD_LOCAL_ONLY)
    assert json.loads(stored.splitlines()[-1])["node_id"] == fix.id


def test_mutation_response_hides_foreign_prerequisite(db, monkeypatch):
    from tests.test_current_plan_bounded_read import _tool_env

    _infra, _fix, lane, _task = _setup(db)
    other = service.add_node(lane.id, kind="task", title="Other", acceptance_criteria="Done", db=db)
    _fence(db)
    tools = _tool_env(db, monkeypatch)
    result = tools._mutation_operation(
        "lane", {"user_id": "user", "project_id": "project"}, None, lane.id, "close_node",
        {"node_id": other.id, "reason": "Done", "expected_revision": other.revision},
        "privacy-mutation", {},
    )
    assert "Fix the dev test" not in json.dumps(result.result)
    assert WITHHELD_LOCAL_ONLY in json.dumps(result.result["next"])
