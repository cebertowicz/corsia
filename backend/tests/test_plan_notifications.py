"""Red-first P5 contract tests for recoverable post-commit plan invalidation."""

from __future__ import annotations

from collections.abc import Iterator

import asyncio
import json
import logging
import sqlite3
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from automation.continuation.sources.harvest import HarvestSource
from infra.schema import SCHEMA_SQL
from plans import mutations, service
from plans.boundary import PlanActor, mutate
from tests.plan_review_helpers import submit_review_evidence


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project-1','Project','t','t')"
    )
    for thread_id, user_id, spawned_by in (
        ("parent", "user-1", None),
        ("child", "user-1", "parent"),
        ("foreign", "user-2", None),
    ):
        connection.execute(
            """INSERT INTO threads
               (id,user_id,project_id,spawned_by,model,created_at,updated_at)
               VALUES (?,?, 'project-1',?,'test/model','t','t')""",
            (thread_id, user_id, spawned_by),
        )
    connection.commit()
    yield connection
    connection.close()


def _notifications():
    from plans import notifications

    return notifications


class _FakeSocket:
    def __init__(self):
        self.sent: list[dict] = []
        self.delivered = threading.Event()

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)
        self.delivered.set()


def _connect(path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _seed_delivery_db(path) -> str:
    connection = _connect(path)
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project-1','Project','t','t')"
    )
    connection.execute(
        """INSERT INTO threads
           (id,user_id,project_id,model,created_at,updated_at)
           VALUES ('parent','user-1','project-1','test/model','t','t')"""
    )
    plan = service.ensure_plan("thread", "parent", db=connection)
    connection.commit()
    connection.close()
    return plan.id


@pytest.fixture
def ws_registry():
    from infra.state import ws as ws_state

    previous = dict(ws_state._ws_user_clients)
    ws_state._ws_user_clients.clear()
    try:
        yield ws_state
    finally:
        ws_state._ws_user_clients.clear()
        ws_state._ws_user_clients.update(previous)


def _task(plan_id: str, title: str, *, db, **kwargs):
    return service.add_node(
        plan_id,
        kind="task",
        title=title,
        acceptance_criteria=f"{title} DOD",
        db=db,
        **kwargs,
    )


def _accept(node_id: str, *, db) -> None:
    service.transition_node(node_id, "active", db=db)
    submit_review_evidence(node_id, db=db)
    service.transition_node(
        node_id,
        "accepted",
        verification_result="passed",
        verified_by="reviewer",
        db=db,
    )


def test_facade_broadcasts_content_free_owner_scope_revisions_only_after_commit(
    db, monkeypatch, caplog,
):
    notifications = _notifications()
    plan = service.ensure_plan("thread", "parent", db=db)
    db.commit()
    sent = []
    monkeypatch.setattr(
        notifications.state,
        "broadcast_to_user",
        lambda user_id, message: sent.append((user_id, message)),
    )

    db.execute("BEGIN")
    db.execute(
        "UPDATE plans SET revision=revision+1,updated_at=? WHERE id=?",
        (service._now(), plan.id),
    )
    with caplog.at_level(logging.ERROR, logger="plans.notifications"):
        assert notifications.notify_after_commit([plan.id], db=db) is False
    assert sent == []
    assert any(
        "caller contract violation" in record.message for record in caplog.records
    )
    db.commit()

    assert notifications.notify_after_commit([plan.id], db=db) is True
    assert sent == [(
        "user-1",
        {
            "type": "plans_changed",
            "contract_version": 1,
            "plans": [{
                "plan_id": plan.id,
                "revision": plan.revision + 1,
                "owner": {"kind": "thread", "id": "parent"},
                "scope": {
                    "workspace_id": notifications.config.WORKSPACE_SELECTION.workspace_id,
                    "project_id": "project-1",
                },
            }],
        },
    )]
    serialized = repr(sent)
    assert "title" not in serialized
    assert "acceptance" not in serialized
    assert "node" not in serialized
    assert "user-2" not in serialized


def test_actor_bound_mutation_notifies_once_and_failed_or_replayed_writes_are_silent(
    db, monkeypatch,
):
    notifications = _notifications()
    plan = service.ensure_plan("thread", "parent", db=db)
    db.commit()
    actor = PlanActor("parent", "user-1", "project-1")
    sent = []
    monkeypatch.setattr(
        notifications.state,
        "broadcast_to_user",
        lambda user_id, message: sent.append((user_id, message)),
    )
    request = {
        "kind": "task",
        "title": "Implement",
        "acceptance_criteria": "Focused tests pass",
    }

    deferred = mutate(
        plan.id,
        "add",
        request,
        actor=actor,
        provenance="test",
        mutation_id="mutation-1",
        db=db,
    )
    assert isinstance(deferred, notifications.DeferredMutation)
    assert sent == []
    result = notifications.deliver_deferred(deferred)
    assert len(sent) == 1
    assert sent[0][1]["plans"][0]["revision"] == service._plan_row(db, plan.id)["revision"]

    replay_deferred = mutate(
        plan.id,
        "add",
        request,
        actor=actor,
        provenance="test",
        mutation_id="mutation-1",
        db=db,
    )
    assert isinstance(replay_deferred, notifications.DeferredMutation)
    assert replay_deferred.messages == ()
    replay = notifications.deliver_deferred(replay_deferred)
    assert replay == result
    assert len(sent) == 1

    with pytest.raises(service.PlanError, match="revision conflict"):
        mutate(
            plan.id,
            "activate",
            {"node_id": result["node"]["id"], "expected_revision": 999},
            actor=actor,
            provenance="test",
            mutation_id="mutation-failed",
            db=db,
        )
    assert len(sent) == 1


def test_committed_mutation_result_survives_broadcast_failure(db, monkeypatch):
    notifications = _notifications()
    plan = service.ensure_plan("thread", "parent", db=db)
    db.commit()
    actor = PlanActor("parent", "user-1", "project-1")

    def fail_broadcast(_user_id, _message):
        raise RuntimeError("socket unavailable")

    monkeypatch.setattr(notifications.state, "broadcast_to_user", fail_broadcast)

    deferred = mutate(
        plan.id,
        "add",
        {
            "kind": "task",
            "title": "Committed",
            "acceptance_criteria": "Mutation result returns",
        },
        actor=actor,
        provenance="test",
        mutation_id="broadcast-failure",
        db=db,
    )
    assert isinstance(deferred, notifications.DeferredMutation)
    result = notifications.deliver_deferred(deferred)

    assert result["node"]["title"] == "Committed"
    assert service.get_node(result["node"]["id"], db=db).title == "Committed"
    assert db.execute(
        "SELECT COUNT(*) FROM plan_mutation_receipts WHERE mutation_id=?",
        ("broadcast-failure",),
    ).fetchone()[0] == 1


def test_api_mutation_via_real_db_worker_reaches_registered_socket(
    tmp_path, monkeypatch, ws_registry,
):
    from api import plans as api_plans

    db_path = tmp_path / "plans-api.db"
    plan_id = _seed_delivery_db(db_path)
    monkeypatch.setattr(api_plans, "get_db", lambda: _connect(db_path))
    socket = _FakeSocket()
    ws_registry._ws_user_clients["user-1"] = [socket]
    app = FastAPI()
    app.include_router(api_plans.router)
    app.dependency_overrides[api_plans.require_user] = lambda: {"id": "user-1"}

    with TestClient(app) as client:
        response = client.post(
            f"/api/plans/{plan_id}/mutations",
            json={
                "operation": "add",
                "mutation_id": "api-real-worker",
                "params": {
                    "kind": "task",
                    "title": "Deliver",
                    "acceptance_criteria": "Socket receives the nudge",
                },
            },
        )

    assert response.status_code == 200, response.text
    assert socket.delivered.wait(1)
    assert [message["type"] for message in socket.sent] == ["plans_changed"]
    assert socket.sent[0]["plans"][0]["plan_id"] == plan_id


@pytest.mark.asyncio
async def test_tool_mutation_via_real_db_worker_reaches_registered_socket(
    tmp_path, monkeypatch, ws_registry,
):
    from tools import plans as tool_plans

    db_path = tmp_path / "plans-tool.db"
    plan_id = _seed_delivery_db(db_path)
    monkeypatch.setattr(tool_plans, "get_db", lambda: _connect(db_path))
    monkeypatch.setattr(
        tool_plans,
        "_require_context",
        lambda _kwargs: (
            "parent",
            {"user_id": "user-1", "project_id": "project-1"},
        ),
    )
    socket = _FakeSocket()
    ws_registry._ws_user_clients["user-1"] = [socket]

    result = await tool_plans.PlanUpdateTool().execute(
        plan_id=plan_id,
        mutation_id="tool-real-worker",
        operation="add",
        params={
            "kind": "task",
            "title": "Deliver",
            "acceptance_criteria": "Socket receives the nudge",
        },
    )
    await asyncio.sleep(0)

    assert json.loads(result)["node"]["title"] == "Deliver"
    assert [message["type"] for message in socket.sent] == ["plans_changed"]
    assert socket.sent[0]["plans"][0]["plan_id"] == plan_id


@pytest.mark.asyncio
async def test_plan_writer_thread_delivers_one_frame_on_the_application_loop(
    tmp_path, ws_registry,
):
    """A committed Plans mutation can notify a pane from its DB worker thread."""
    from infra import app_loop
    from plans import notifications

    db_path = tmp_path / "plans-writer-thread.db"
    plan_id = _seed_delivery_db(db_path)
    socket = _FakeSocket()
    ws_registry._ws_user_clients["user-1"] = [socket]
    errors: list[BaseException] = []

    def write_and_deliver() -> None:
        connection = _connect(db_path)
        try:
            deferred = mutate(
                plan_id,
                "add",
                {
                    "kind": "task",
                    "title": "Deliver from a worker",
                    "acceptance_criteria": "The plan pane receives one frame",
                },
                actor=PlanActor("parent", "user-1", "project-1"),
                provenance="test.worker_thread",
                mutation_id="worker-thread-delivery",
                db=connection,
            )
            notifications.deliver_deferred(deferred)
        except BaseException as exc:  # pragma: no cover - asserted by the test thread
            errors.append(exc)
        finally:
            connection.close()

    app_loop.capture(asyncio.get_running_loop())
    worker = threading.Thread(target=write_and_deliver)
    try:
        worker.start()
        await asyncio.to_thread(worker.join)
        assert errors == []
        assert await asyncio.to_thread(socket.delivered.wait, 1)
    finally:
        app_loop.clear()

    assert [message["type"] for message in socket.sent] == ["plans_changed"]
    assert socket.sent[0]["plans"][0]["plan_id"] == plan_id


def test_delegation_harvest_system_writer_notifies_parent_after_commit(
    db, monkeypatch,
):
    notifications = _notifications()
    parent = service.ensure_plan("thread", "parent", db=db)
    parent_node = _task(
        parent.id,
        "Delegate",
        spawn_kind="claude-code",
        db=db,
    )
    service.transition_node(parent_node.id, "active", db=db)
    child = service.ensure_plan("thread", "child", db=db)
    child_node = _task(child.id, "Child result", db=db)
    mutations.attach_spawned_thread(parent_node.id, "child", db=db)
    sent = []
    monkeypatch.setattr(
        notifications.state,
        "broadcast_to_user",
        lambda user_id, message: sent.append((user_id, message)),
    )

    _accept(child_node.id, db=db)
    sent.clear()
    assert HarvestSource().prepare("2026-09-22T00:00:00+00:00", db=db) == [
        "parent"
    ]

    assert len(sent) == 1
    user_id, message = sent[0]
    assert user_id == "user-1"
    assert message["type"] == "plans_changed"
    assert message["plans"] == [{
        "plan_id": parent.id,
        "revision": service._plan_row(db, parent.id)["revision"],
        "owner": {"kind": "thread", "id": "parent"},
        "scope": {
            "workspace_id": notifications.config.WORKSPACE_SELECTION.workspace_id,
            "project_id": "project-1",
        },
    }]


def test_api_pause_reaches_registered_socket_with_the_paused_plan(
    tmp_path, monkeypatch, ws_registry,
):
    """A whole-plan pause nudges the chat list like any other plan change.

    The chat list shows a paused plan as a pause, and it learns that from this
    nudge; a pause that never arrives leaves the row showing the state the plan
    had before the user paused it.
    """
    from api import plans as api_plans

    db_path = tmp_path / "plans-pause-api.db"
    plan_id = _seed_delivery_db(db_path)
    connection = _connect(db_path)
    revision = int(connection.execute(
        "SELECT revision FROM plans WHERE id=?", (plan_id,),
    ).fetchone()["revision"])
    connection.close()
    monkeypatch.setattr(api_plans, "get_db", lambda: _connect(db_path))
    socket = _FakeSocket()
    ws_registry._ws_user_clients["user-1"] = [socket]
    app = FastAPI()
    app.include_router(api_plans.router)
    app.dependency_overrides[api_plans.require_user] = lambda: {"id": "user-1"}

    with TestClient(app) as client:
        response = client.post(
            f"/api/plans/{plan_id}/mutations",
            json={
                "operation": "pause",
                "mutation_id": "api-real-worker-pause",
                "params": {"expected_revision": revision},
            },
        )

    assert response.status_code == 200, response.text
    assert socket.delivered.wait(1)
    assert [message["type"] for message in socket.sent] == ["plans_changed"]
    assert socket.sent[0]["plans"][0]["plan_id"] == plan_id
    connection = _connect(db_path)
    assert connection.execute(
        "SELECT state FROM plans WHERE id=?", (plan_id,),
    ).fetchone()["state"] == "paused"
    connection.close()
