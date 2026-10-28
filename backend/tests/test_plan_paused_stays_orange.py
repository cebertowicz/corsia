"""Paused asks retain their pane display without raising attention."""
import pytest

from plans import artifact_links, operator_projection, service
from registry import attention as registry_attention, models
from tests.test_plan_decisions import _task
from tests.test_plan_pause_defers_person_ask import (
    _human, _lifecycle, _node_lifecycle, _raised_decision, db,
)


@pytest.mark.parametrize("kind", ["task", "review", "decision", "artifact"])
@pytest.mark.parametrize("pause", ["item", "plan"])
def test_paused_ask_stays_orange(db, kind, pause, monkeypatch):
    plan = service.ensure_plan("thread", "owner", db=db)
    parent = service.add_node(plan.id, kind="phase", title="Parent", acceptance_criteria="Done", db=db)
    if kind == "decision":
        import tests.test_plan_decision_answer as decision_helpers
        monkeypatch.setattr(decision_helpers, "_task", lambda plan_id, title, *, db: _task(plan_id, title, parent_id=parent.id, db=db))
        plan, _, decision = _raised_decision(db)
        node_id = decision["node_id"]
        parent_id = parent.id
    else:
        node = _task(plan.id, "Ask", parent_id=parent.id, assignee="user" if kind in {"task", "review"} else None, db=db)
        node_id, parent_id = node.id, parent.id
        db.commit()
        if kind == "review":
            _node_lifecycle(db, plan.id, node_id, "activate")
            _node_lifecycle(db, plan.id, node_id, "request_review", evidence="Ready")
        if kind == "artifact":
            models.register_artifact(integration="github", type="pr", external_id="orange#1", parent_thread_id="owner", title="Review", db=db)
            artifact = db.execute("SELECT id FROM artifact_identities WHERE external_id='orange#1'").fetchone()[0]
            registry_attention.raise_attention(artifact, db=db)
            artifact_links.link(node_id, artifact, "attention", actor_id=None, provenance="test", db=db)
            db.commit()

    def rows():
        return {n["id"]: n for n in operator_projection.read_thread("owner", actor=_human(), db=db)["plans"][0]["nodes"]}

    assert rows()[node_id]["requires_user_action"]
    if pause == "item":
        _node_lifecycle(db, plan.id, node_id, "pause_node")
    else:
        _lifecycle(db, plan.id, "pause")
    projected = rows()
    orange = {"label_key": "needs_you", "tone_key": "human-review"}
    assert projected[node_id]["display"] == orange
    assert projected[parent_id]["display"] == orange
    assert not projected[node_id]["requires_user_action"]
    assert not projected[parent_id]["requires_user_action"]
    if pause == "item":
        _node_lifecycle(db, plan.id, node_id, "resume_node")
    else:
        _lifecycle(db, plan.id, "resume")
    assert rows()[node_id]["requires_user_action"]


def test_paused_user_task_with_dependency_stays_waiting(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    dependency = _task(plan.id, "Prerequisite", db=db)
    node = _task(plan.id, "Ask", assignee="user", db=db)
    db.commit()
    service.add_dependency(node.id, dependency.id, db=db)
    db.commit()
    _node_lifecycle(db, plan.id, node.id, "pause_node")
    rows = operator_projection.read_thread("owner", actor=_human(), db=db)["plans"][0]["nodes"]
    assert next(row for row in rows if row["id"] == node.id)["display"]["label_key"] == "waiting"
