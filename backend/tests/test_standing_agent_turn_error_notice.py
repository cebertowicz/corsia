"""A standing agent whose Turn ends on an error is not retried; whoever answers for it is told once."""

from __future__ import annotations

import pytest

from automation import plan_liveness_terminal
from infra.database import get_db
from infra.conductor import WORKSPACE_SCOPE
from tests.event_routing_helpers import mk_session

_ERROR = "provider sent no data for 600s"


@pytest.fixture
def told(monkeypatch):
    """Capture every coordinator notice; coordinators come from ``coordinators``."""
    import events.delivery as delivery
    import infra.conductor as conductor
    import infra.plan_limit_continue as limit_continue

    state = {"coordinators": {}, "sent": [], "started": [], "rerun": False}
    monkeypatch.setattr(
        conductor, "get_scope_coordinator",
        lambda scope, db=None: state["coordinators"].get(scope),
    )
    monkeypatch.setattr(
        delivery, "enqueue_event_wake",
        lambda recipient, kind, summary, details, *, message_id, db: state["sent"].append(
            (recipient, kind, summary, details, message_id),
        ),
    )
    monkeypatch.setattr(
        delivery, "start_idle_after_commit", lambda db, thread_id: state["started"].append(thread_id),
    )
    monkeypatch.setattr(limit_continue, "rerun_queued", lambda turn_id, *, db=None: state["rerun"])
    return state


def _agent(thread_id: str, *, project_id: str | None = "proj-1", spawned_by: str | None = None) -> str:
    mk_session(thread_id, project_id=project_id, spawned_by=spawned_by)
    db = get_db()
    db.execute("UPDATE threads SET agent_enabled=1, title=? WHERE id=?", (f"Agent {thread_id}", thread_id))
    db.commit()
    db.close()
    return thread_id


def _tell(thread_id: str, turn_id: str | None = "turn-1") -> None:
    db = get_db()
    plan_liveness_terminal._tell_coordinator(db, thread_id, turn_id=turn_id, error=_ERROR)
    db.close()


def test_a_project_agent_tells_its_conductor_once(isolated_db, told):
    told["coordinators"] = {"proj-1": "conductor", WORKSPACE_SCOPE: "corsia"}
    _agent("lane")

    _tell("lane")
    _tell("lane")  # the same Turn again carries the same message id

    assert [s[0] for s in told["sent"]] == ["conductor", "conductor"]
    assert told["sent"][0][4] == told["sent"][1][4]
    recipient, kind, summary, details, _ = told["sent"][0]
    assert kind == "agent.turn_errored"
    assert "will not retry" in summary
    assert details["error"] == _ERROR and details["thread_id"] == "lane"
    assert details["working_on"] == "no active task"
    assert "model" not in details["next"]  # only the person changes a standing agent's model
    assert told["started"] == ["conductor", "conductor"]


def test_the_real_delivery_queues_one_message_for_a_repeated_turn(isolated_db, monkeypatch):
    import events.delivery as delivery
    import infra.conductor as conductor

    mk_session("conductor", project_id="proj-1")
    _agent("lane")
    monkeypatch.setattr(conductor, "get_scope_coordinator", lambda scope, db=None: "conductor")
    monkeypatch.setattr(delivery, "start_idle_after_commit", lambda db, thread_id: None)

    for _ in range(2):
        db = get_db()
        plan_liveness_terminal._tell_coordinator(db, "lane", turn_id="turn-1", error=_ERROR)
        db.commit()
        db.close()

    db = get_db()
    rows = db.execute(
        "SELECT content FROM messages WHERE thread_id='conductor' AND content LIKE '%will not retry%'",
    ).fetchall()
    db.close()
    assert len(rows) == 1


def test_a_project_with_no_conductor_tells_the_corsia_agent(isolated_db, told):
    told["coordinators"] = {WORKSPACE_SCOPE: "corsia"}
    _agent("lane")

    _tell("lane")

    assert [s[0] for s in told["sent"]] == ["corsia"]


def test_the_conductor_itself_tells_the_corsia_agent(isolated_db, told):
    told["coordinators"] = {"proj-1": "conductor", WORKSPACE_SCOPE: "corsia"}
    _agent("conductor")

    _tell("conductor")

    assert [s[0] for s in told["sent"]] == ["corsia"]


def test_a_workspace_agent_tells_the_corsia_agent(isolated_db, told):
    told["coordinators"] = {WORKSPACE_SCOPE: "corsia"}
    _agent("helper", project_id=None)

    _tell("helper")

    assert [s[0] for s in told["sent"]] == ["corsia"]


def test_the_corsia_agent_itself_tells_no_agent(isolated_db, told):
    told["coordinators"] = {WORKSPACE_SCOPE: "corsia"}
    _agent("corsia", project_id=None)

    _tell("corsia")

    assert told["sent"] == [] and told["started"] == []


def test_a_worker_is_left_to_its_parents_plan_wake(isolated_db, told):
    told["coordinators"] = {"proj-1": "conductor", WORKSPACE_SCOPE: "corsia"}
    mk_session("parent", project_id="proj-1")
    mk_session("worker", project_id="proj-1", spawned_by="parent")

    _tell("worker")

    assert told["sent"] == []


def test_a_turn_with_its_rerun_queued_tells_nobody_yet(isolated_db, told):
    told["coordinators"] = {"proj-1": "conductor", WORKSPACE_SCOPE: "corsia"}
    told["rerun"] = True
    _agent("lane")

    _tell("lane")

    assert told["sent"] == []
