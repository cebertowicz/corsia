"""Delegation notices respect the recipient's current project fence."""

import json

import pytest

from infra.project_fence import WITHHELD_LOCAL_ONLY
from plans import closure, service
from plans.delegation_notice import _wake_parent
from tests.child_parent_notice_helpers import db, delegated, notice_details, notices, wakes  # noqa: F401


def _fence(db):
    db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES ('secret','Secret','t','t')")
    db.execute("INSERT INTO project_inference_policies(project_id,cloud_models_off) VALUES ('secret',1)")
    db.execute("UPDATE threads SET project_id='secret' WHERE id='worker'")
    db.execute("UPDATE threads SET project_id=NULL WHERE id='parent'")
    db.execute("INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES ('corsia_agent_thread_id',?,'t')",
               (json.dumps({"session_id": "parent"}),))
    db.commit()


@pytest.mark.parametrize("fenced", [True, False])
def test_parent_wake_projects_child_content(db, fenced):
    step, child_plan, child = delegated(db, title="Secret worker title")
    if fenced:
        _fence(db)
    _wake_parent(db, step.plan_id, "worker", kind="child_stopped_open", subject_id=child_plan.id,
                 revision=1, extra={"private": "Secret extra", "node_id": child.id})
    content = wakes(db)[0]["content"]
    line, encoded = content.splitlines()
    details = json.loads(encoded)
    assert details["node_id"] == child.id
    assert details["worker_thread_id"] == "worker"
    if fenced:
        assert line == WITHHELD_LOCAL_ONLY
        assert details["private"] == WITHHELD_LOCAL_ONLY
    else:
        assert details["private"] == "Secret extra"
        assert line != WITHHELD_LOCAL_ONLY


def test_passive_child_digest_projects_title_and_reason(db):
    _step, _plan, node = delegated(db, title="Secret title")
    _fence(db)
    closure.close_node(node.id, reason="Secret reason", actor_id="worker", db=db)
    notice = notices(db)[-1]
    assert notice["summary"] == WITHHELD_LOCAL_ONLY
    details = notice_details(notice)
    assert details["reason"] == WITHHELD_LOCAL_ONLY
    assert details["node_id"] == node.id


def test_returned_worker_steps_withhold_evidence(db, monkeypatch):
    from plans import delegation_harvest, plan_wake, plan_wake_returned, review_evidence

    step, child_plan, child = delegated(db, title="Secret title")
    _fence(db)
    leaf = service._node_row(db, child.id)
    monkeypatch.setattr(delegation_harvest, "return_rows", lambda *_args: [leaf])
    monkeypatch.setattr(review_evidence, "latest", lambda *_args, **_kwargs: {
        "evidence": "Secret evidence", "artifact_ids": ["artifact-1"],
    })
    sent = {}
    monkeypatch.setattr(plan_wake, "append", lambda _db, **kwargs: sent.update(kwargs) or "wake")
    plan_wake_returned.notify(db, node_id=step.id, child_plan_id=child_plan.id, worker_thread_id="worker")
    content = sent["content"]
    assert "Secret" not in content
    returned = json.loads(content.splitlines()[-1])["returned"]
    assert returned["steps"][0]["id"] == child.id
    assert returned["steps"][0]["evidence"] == WITHHELD_LOCAL_ONLY
    assert returned["steps"][0]["artifact_ids"] == ["artifact-1"]
