"""A watch routed to another thread cannot carry a fenced task's text."""

import json

import pytest

from automation import watches
from automation.continuation.execution import event_for
from automation.continuation.sources.watch_expiry import WatchExpirySource
from infra.project_fence import WITHHELD_LOCAL_ONLY
from plans import watch_operations
from tests.test_plan_scheduled_wait import _task, db  # noqa: F401


@pytest.mark.parametrize("fenced", [True, False])
def test_watch_expiry_projects_its_source_plan(db, fenced):
    plan, node = _task(db)
    db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) VALUES ('parent','user','m','t','t')")
    db.execute("INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES ('corsia_agent_thread_id',?,'t')",
               (json.dumps({"session_id": "parent"}),))
    if fenced:
        db.execute("INSERT INTO project_inference_policies(project_id,cloud_models_off) VALUES ('project',1)")
    watch = watches.arm_watch(
        node.id, {"type": "deadline", "value": "2999-01-01T00:00:00+00:00"},
        owner_thread="parent", expected_event="scheduled", plan_id=plan.id, plan_node_id=node.id, db=db,
    )
    watch_operations.record_armed(
        db, node.id, watch, blocked=None, scheduled={"reason": "Secret reason"},
        actor_id="owner", provenance="test",
    )
    [request] = WatchExpirySource().due("3000-01-01T00:00:00+00:00", db=db)
    _event, summary, details = event_for(request)
    assert details["watch_id"] == watch.id
    assert details["plan_node_id"] == node.id
    if fenced:
        assert summary == WITHHELD_LOCAL_ONLY
    else:
        assert "Secret reason" in summary
        assert "Tidy the index" in summary
