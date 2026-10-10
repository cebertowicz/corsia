"""The user's Mark done and archive reach the server and take effect.

The pane posts each control to the generic mutations route with the body
`mutateUserTask` builds. This drives that exact request, so a refusal the
service-level tests cannot see (authorization, validation, the mutation id)
fails here.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_attention_queue import _mid
from tests.test_attention_queue_api import _DbProxy, _immediate
from tests.test_plan_archival import _plan, _row, _task, db  # noqa: F401 - fixture


def _client(db, monkeypatch) -> TestClient:
    from api import plans as api_plans

    monkeypatch.setattr(api_plans, "get_db", lambda: _DbProxy(db))
    monkeypatch.setattr(api_plans, "run_in_db", _immediate)
    app = FastAPI()
    app.include_router(api_plans.router)
    app.dependency_overrides[api_plans.require_user] = lambda: {"id": "user-1"}
    return TestClient(app)


def _post(client: TestClient, plan_id: str, action: str, node) -> object:
    revision = node["revision"]
    return client.post(
        f"/api/plans/{plan_id}/mutations",
        json={
            "operation": action,
            "mutation_id": f"{action}:{node['id']}:{revision}",
            "params": {"node_id": node["id"], "expected_revision": revision},
        },
    )


def test_mark_done_on_a_user_task_accepts_it(db, monkeypatch):
    client = _client(db, monkeypatch)
    plan = _plan(db)
    node = _task(db, plan.id, title="Sign off", assignee="user")

    response = _post(client, plan.id, "mark_done", _row(db, node.id))

    assert response.status_code == 200, response.text
    assert _row(db, node.id)["state"] == "accepted"


def _owner_update(db, plan_id: str, operation: str, params: dict) -> None:
    from plans.boundary import mutate
    from tests.plan_ask_helpers import with_actionable_user_ask
    from tests.test_plan_archival import OWNER

    params = with_actionable_user_ask(db, plan_id, operation, params, OWNER)
    mutate(plan_id, operation, params, actor=OWNER, provenance="test",
           mutation_id=_mid(), db=db)
    db.commit()


def _cleared_by_the_user(db, node_id: str, event: str) -> bool:
    """The cause ended as the same actor who accepted the task."""
    rows = db.execute(
        """SELECT event, new_state, actor_id FROM plan_node_events
           WHERE node_id=? AND (event=? OR new_state='accepted')""",
        (node_id, event),
    ).fetchall()
    cleared = {row["actor_id"] for row in rows if row["event"] == event}
    accepted = {row["actor_id"] for row in rows if row["new_state"] == "accepted"}
    return bool(cleared) and cleared == accepted


def test_mark_done_on_a_user_task_blocked_on_the_user_accepts_it(db, monkeypatch):
    client = _client(db, monkeypatch)
    plan = _plan(db)
    node = _task(db, plan.id, title="Sign off", assignee="user")
    _owner_update(db, plan.id, "block_node", {
        "node_id": node.id, "expected_revision": _row(db, node.id)["revision"],
        "owner_kind": "user", "reason": "Needs Steve", "reason_key": "needs_user",
    })

    response = _post(client, plan.id, "mark_done", _row(db, node.id))

    assert response.status_code == 200, response.text
    assert _row(db, node.id)["state"] == "accepted"
    assert _cleared_by_the_user(db, node.id, "hold_cleared")


def test_mark_done_on_a_user_task_behind_an_open_dependency_accepts_it(db, monkeypatch):
    client = _client(db, monkeypatch)
    plan = _plan(db)
    first = _task(db, plan.id, title="First")
    node = _task(db, plan.id, title="Sign off", assignee="user")
    _owner_update(db, plan.id, "add_dependency", {
        "node_id": node.id, "expected_revision": _row(db, node.id)["revision"],
        "depends_on_node_id": first.id,
    })

    response = _post(client, plan.id, "mark_done", _row(db, node.id))

    assert response.status_code == 200, response.text
    assert _row(db, node.id)["state"] == "accepted"
    assert _cleared_by_the_user(db, node.id, "dependency_removed")
    assert _row(db, first.id)["state"] == "pending"


def test_mark_done_on_a_user_task_in_a_paused_plan_accepts_it(db, monkeypatch):
    client = _client(db, monkeypatch)
    plan = _plan(db)
    node = _task(db, plan.id, title="Sign off", assignee="user")
    plan_revision = db.execute(
        "SELECT revision FROM plans WHERE id=?", (plan.id,),
    ).fetchone()["revision"]
    paused = client.post(f"/api/plans/{plan.id}/mutations", json={
        "operation": "pause", "mutation_id": _mid(),
        "params": {"expected_revision": plan_revision},
    })
    assert paused.status_code == 200, paused.text

    response = _post(client, plan.id, "mark_done", _row(db, node.id))

    assert response.status_code == 200, response.text
    assert _row(db, node.id)["state"] == "accepted"
    assert db.execute(
        "SELECT state FROM plans WHERE id=?", (plan.id,),
    ).fetchone()["state"] == "paused"


def test_mark_done_on_a_user_task_with_an_armed_watch_accepts_it(db, monkeypatch):
    from tests.test_plan_archival import _DEADLINE

    client = _client(db, monkeypatch)
    plan = _plan(db)
    node = _task(db, plan.id, title="Sign off", assignee="user")
    _owner_update(db, plan.id, "arm_watch", {
        "node_id": node.id, "expected_revision": _row(db, node.id)["revision"],
        "condition": _DEADLINE, "expected_event": "external.done",
    })

    response = _post(client, plan.id, "mark_done", _row(db, node.id))

    assert response.status_code == 200, response.text
    assert _row(db, node.id)["state"] == "accepted"
    assert db.execute(
        "SELECT COUNT(*) FROM expected_event_watches "
        "WHERE plan_node_id=? AND status='armed'", (node.id,),
    ).fetchone()[0] == 0


def test_archive_on_a_user_task_blocked_on_the_user_removes_it(db, monkeypatch):
    client = _client(db, monkeypatch)
    plan = _plan(db)
    node = _task(db, plan.id, title="Sign off", assignee="user")
    _owner_update(db, plan.id, "block_node", {
        "node_id": node.id, "expected_revision": _row(db, node.id)["revision"],
        "owner_kind": "user", "reason": "Needs Steve", "reason_key": "needs_user",
    })

    response = _post(client, plan.id, "archive_node", _row(db, node.id))

    assert response.status_code == 200, response.text
    assert _row(db, node.id)["archived_at"] is not None


def test_archive_on_a_user_task_behind_an_open_dependency_removes_it(db, monkeypatch):
    client = _client(db, monkeypatch)
    plan = _plan(db)
    first = _task(db, plan.id, title="First")
    node = _task(db, plan.id, title="Sign off", assignee="user")
    _owner_update(db, plan.id, "add_dependency", {
        "node_id": node.id, "expected_revision": _row(db, node.id)["revision"],
        "depends_on_node_id": first.id,
    })

    response = _post(client, plan.id, "archive_node", _row(db, node.id))

    assert response.status_code == 200, response.text
    assert _row(db, node.id)["archived_at"] is not None


def test_archive_on_a_user_task_removes_it(db, monkeypatch):
    client = _client(db, monkeypatch)
    plan = _plan(db)
    node = _task(db, plan.id, title="Sign off", assignee="user")

    response = _post(client, plan.id, "archive_node", _row(db, node.id))

    assert response.status_code == 200, response.text
    assert _row(db, node.id)["archived_at"] is not None
