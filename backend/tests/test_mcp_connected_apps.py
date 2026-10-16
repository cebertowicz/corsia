from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from connections.app_lifecycle import create_app, create_pending_app
from connections.app_reach import get_app, key_entry_names
from infra.database import initialize_schema


@pytest.fixture
def connected_apps_client(monkeypatch):
    from api import connected_apps, mcp
    from infra.auth import require_user

    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    for project_id in ("project-a", "project-b"):
        db.execute(
            "INSERT INTO projects(id,name,created_at,updated_at) VALUES (?,?,?,?)",
            (project_id, project_id, "t", "t"),
        )
    for thread_id, project_id in (("thread-a", "project-a"), ("thread-b", "project-b"), ("workspace", None)):
        db.execute(
            "INSERT INTO threads(id,user_id,project_id,created_at,updated_at) VALUES (?,?,?,?,?)",
            (thread_id, "user", project_id, "t", "t"),
        )
    db.commit()

    async def immediate(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    actor = {"id": "user", "thread_id": "thread-a", "token_kind": "internal"}
    app = FastAPI()
    app.include_router(mcp.router)
    app.dependency_overrides[require_user] = lambda: actor
    async def runtime_noop(_action: str, _app_id: str) -> dict[str, object]:
        return {"state": "disconnected", "state_since": "t", "tool_count": 0, "failure_class": None}

    monkeypatch.setattr(connected_apps, "run_in_db", immediate)
    monkeypatch.setattr(connected_apps, "_db", lambda: db)
    monkeypatch.setattr(connected_apps, "_run_app_runtime", runtime_noop)
    with TestClient(app) as client:
        yield client, db, actor
    db.close()


def test_agent_list_hides_unreached_configuration(connected_apps_client) -> None:
    client, db, _actor = connected_apps_client
    reached = create_app(
        "A", {"command": "a", "key_refs": [{"kind": "env", "name": "A_TOKEN"}]},
        {"mode": "selected", "project_ids": ["project-a"], "outside_projects": False}, db=db,
    )
    unreachable = create_app(
        "B", {"command": "b", "key_refs": [{"kind": "env", "name": "B_TOKEN"}]},
        {"mode": "selected", "project_ids": ["project-b"], "outside_projects": False}, db=db,
    )

    response = client.get("/api/mcp/servers")

    assert response.status_code == 200
    rows = response.json()["servers"]
    by_name = {row["name"]: row for row in rows}
    assert by_name["A"]["id"] == reached["id"]
    assert by_name["A"]["config"]["command"] == "a"
    assert by_name["A"]["key_refs"] == [{"kind": "env", "name": "A_TOKEN", "set": False}]
    assert by_name["A"]["on_here"] is True
    assert by_name["A"]["next_step"] == (
        "A lost its connection. Open A in project-a's Connected apps and press Reconnect."
    )
    assert by_name["B"] == {
        "name": "B", "state": "disconnected", "on_here": False,
        "next_step": "B is turned off for this project; ask the person to turn it on in Connected apps.",
    }

    refused = client.patch(
        f"/api/mcp/servers/{unreachable['id']}",
        json={"config": {"command": "changed"}, "key_refs": [], "expected_revision": 1},
    )
    assert refused.status_code == 403


def test_agent_state_guidance_uses_the_persons_connected_apps_words():
    from api.connected_apps import _agent_next_step

    place = "Research's Connected apps"
    cases = {
        "awaiting_approval": "Ask the person to approve GitHub in Research's Connected apps.",
        "expired": (
            "My request to connect GitHub expired after seven days without an answer. "
            "Do you still want it? If so, I'll send it again."
        ),
        "connecting": "Wait for the connection attempt to finish.",
        "working": 'Load its tools with tools_load(server="GitHub").',
        "needs_token": "GitHub needs a key. Press Set key next to GitHub in Research's Connected apps. Please don't paste it here.",
        "token_rejected": "GitHub didn't accept its key. Press Replace key next to GitHub in Research's Connected apps.",
        "disconnected": "GitHub lost its connection. Open GitHub in Research's Connected apps and press Reconnect.",
        "failed": (
            "Reconnect GitHub once. If it fails again, tell the person: GitHub couldn't connect: no response. "
            "I tried once more and it still didn't connect. Open GitHub in Research's Connected apps to see why."
        ),
    }

    for state, expected in cases.items():
        assert _agent_next_step(
            "GitHub", state, "timed_out", [{"kind": "env", "name": "GITHUB_TOKEN", "set": False}],
            on_here=True, place=place,
        ) == expected
    for failure_class, plain_cause in {
        "cannot_start": "app didn't start",
        "timed_out": "no response",
        "protocol_error": "not compatible",
        "name_collision": "clashes with another app",
    }.items():
        step = _agent_next_step("GitHub", "failed", failure_class, [], on_here=True, place=place)
        assert plain_cause in step
        assert failure_class not in step


def test_agent_proposal_needs_person_and_preserves_active_config(connected_apps_client) -> None:
    client, db, actor = connected_apps_client
    original = create_app(
        "A", {"command": "old", "key_refs": []},
        {"mode": "selected", "project_ids": ["project-a"], "outside_projects": False}, db=db,
    )

    proposal = client.patch(
        f"/api/mcp/servers/{original['id']}",
        json={"config": {"command": "new"}, "key_refs": [], "expected_revision": 1},
    )
    assert proposal.status_code == 200
    assert get_app(original["id"], db=db)["config"]["command"] == "old"
    assert get_app(original["id"], db=db)["state"] == "awaiting_approval"

    refused = client.post(f"/api/mcp/servers/{original['id']}/approve")
    assert refused.status_code == 403

    actor.clear()
    actor.update({"id": "user", "token_kind": "session"})
    accepted = client.post(f"/api/mcp/servers/{original['id']}/approve")
    assert accepted.status_code == 200
    approved = get_app(original["id"], db=db)
    assert approved["config"]["command"] == "new"
    assert approved["state"] == "disconnected"


def test_agent_create_uses_server_thread_scope_and_refuses_reach(connected_apps_client) -> None:
    client, _db, _actor = connected_apps_client
    refused = client.post(
        "/api/mcp/servers",
        json={
            "name": "A", "config": {"command": "a"}, "key_refs": [],
            "reach": {"mode": "all", "project_ids": [], "outside_projects": False},
        },
    )
    assert refused.status_code == 403

    created = client.post(
        "/api/mcp/servers",
        json={"name": "A", "config": {"command": "a"}, "key_refs": []},
    )
    assert created.status_code == 201
    assert created.json()["reach"] == {
        "mode": "selected", "project_ids": ["project-a"], "outside_projects": False,
    }


def test_workspace_corsia_proposes_an_app_for_all_projects(connected_apps_client) -> None:
    client, db, actor = connected_apps_client
    actor["thread_id"] = "workspace"
    db.execute(
        "INSERT INTO settings(key,value,updated_at) VALUES ('corsia_agent_thread_id', ?, 't')",
        ('{"session_id":"workspace"}',),
    )
    db.commit()

    created = client.post(
        "/api/mcp/servers", json={"name": "Workspace app", "config": {"command": "app"}, "key_refs": []},
    )

    assert created.status_code == 201
    assert created.json()["reach"] == {"mode": "all", "project_ids": [], "outside_projects": False}


def test_local_app_connect_returns_the_approved_mac_notice(connected_apps_client) -> None:
    client, _db, actor = connected_apps_client
    actor.clear()
    actor.update({"id": "user", "token_kind": "session"})

    response = client.post(
        "/api/mcp/servers",
        json={
            "name": "A", "config": {"command": "a"}, "key_refs": [],
            "reach": {"mode": "selected", "project_ids": ["project-a"], "outside_projects": False},
        },
    )

    assert response.status_code == 201
    assert response.json()["notice"] == (
        "It can do anything you can do on this Mac, except open CORSIA's own files and saved keys. "
        "Connect only apps you trust."
    )


def test_persisted_route_changes_use_the_stable_app_runtime_seam(connected_apps_client, monkeypatch) -> None:
    from api import connected_apps

    client, _db, actor = connected_apps_client
    actions: list[tuple[str, str]] = []

    async def record(action: str, app_id: str) -> None:
        actions.append((action, app_id))

    monkeypatch.setattr(connected_apps, "_run_app_runtime", record)
    actor.clear()
    actor.update({"id": "user", "token_kind": "session"})
    created = client.post(
        "/api/mcp/servers",
        json={
            "name": "A", "config": {"command": "old"}, "key_refs": [],
            "reach": {"mode": "selected", "project_ids": ["project-a"], "outside_projects": False},
        },
    )
    assert created.status_code == 201
    app_id = created.json()["id"]
    changed = client.patch(
        f"/api/mcp/servers/{app_id}",
        json={"config": {"command": "new"}, "key_refs": [], "expected_revision": 1},
    )
    assert changed.status_code == 200
    removed = client.delete(f"/api/mcp/servers/{app_id}")
    assert removed.status_code == 200
    assert actions == [("connect", app_id), ("reconnect", app_id), ("remove", app_id)]


def test_reconnect_uses_the_app_runtime_and_returns_its_live_state(connected_apps_client, monkeypatch) -> None:
    from api import connected_apps

    client, db, actor = connected_apps_client
    app = create_app(
        "A", {"command": "a", "key_refs": []},
        {"mode": "selected", "project_ids": ["project-a"], "outside_projects": False}, db=db,
    )
    calls = []

    async def reconnect(action: str, app_id: str) -> dict[str, object]:
        calls.append((action, app_id))
        return {"state": "working", "state_since": "now", "tool_count": 2, "failure_class": None}

    monkeypatch.setattr(connected_apps, "_run_app_runtime", reconnect)
    response = client.post(f"/api/mcp/servers/{app['id']}/reconnect")

    assert response.status_code == 200
    assert response.json() == {
        "id": app["id"], "state": "working", "state_since": "now", "tool_count": 2, "failure_class": None,
    }
    assert calls == [("reconnect", app["id"])]


def test_expired_proposal_is_listed_and_cannot_be_approved(connected_apps_client) -> None:
    client, db, actor = connected_apps_client
    db.execute("UPDATE threads SET title='Planning' WHERE id='thread-a'")
    db.commit()
    proposal = create_pending_app(
        "A", {"command": "a", "key_refs": []},
        {"mode": "selected", "project_ids": ["project-a"], "outside_projects": False},
        actor_thread_id="thread-a", db=db,
    )
    db.execute("UPDATE connected_apps SET state_since='2000-01-01T00:00:00' WHERE id=?", (proposal["id"],))
    db.commit()

    listed = client.get("/api/mcp/servers")
    assert listed.status_code == 200
    row = listed.json()["servers"][0]
    assert row["id"] == proposal["id"]
    assert row["state"] == "expired"
    assert row["config"] == {"command": "a"}
    assert row["key_refs"] == []
    assert row["pending_actor_name"] == "Planning"
    assert row["on_here"] is True

    actor.clear()
    actor.update({"id": "user", "token_kind": "session"})
    refused = client.post(f"/api/mcp/servers/{proposal['id']}/approve")
    assert refused.status_code == 409
    assert refused.json()["detail"] == "This request expired after seven days. Ask Planning to send it again."
    assert db.execute("SELECT 1 FROM connected_apps WHERE id=?", (proposal["id"],)).fetchone() is None

    dismissed = create_pending_app(
        "B", {"command": "b", "key_refs": []},
        {"mode": "selected", "project_ids": ["project-a"], "outside_projects": False},
        actor_thread_id="thread-a", db=db,
    )
    db.execute("UPDATE connected_apps SET state_since='2000-01-01T00:00:00' WHERE id=?", (dismissed["id"],))
    db.commit()
    decline = client.post(f"/api/mcp/servers/{dismissed['id']}/decline")
    assert decline.status_code == 200
    assert decline.json() == {"id": dismissed["id"], "removed": True}


def test_human_reach_change_is_revision_checked_and_key_refs_never_accept_values(connected_apps_client) -> None:
    client, db, actor = connected_apps_client
    actor.clear()
    actor.update({"id": "user", "token_kind": "session"})
    app = create_app(
        "A", {"command": "a", "key_refs": []},
        {"mode": "selected", "project_ids": ["project-a"], "outside_projects": False}, db=db,
    )

    changed = client.put(
        f"/api/mcp/servers/{app['id']}/reach",
        json={
            "expected_revision": 1,
            "reach": {"mode": "selected", "project_ids": ["project-b"], "outside_projects": False},
        },
    )
    assert changed.status_code == 200
    assert changed.json()["reach"]["project_ids"] == ["project-b"]

    invalid_ref = client.post(
        "/api/mcp/servers",
        json={
            "name": "B", "config": {"command": "b"},
            "key_refs": [{"kind": "env", "name": "B_TOKEN", "value": "not-allowed"}],
            "reach": {"mode": "selected", "project_ids": ["project-a"], "outside_projects": False},
        },
    )
    assert invalid_ref.status_code == 422


def test_connected_app_add_and_edit_never_persist_a_literal_credential(connected_apps_client) -> None:
    client, db, actor = connected_apps_client
    actor.clear()
    actor.update({"id": "user", "token_kind": "session"})
    literal = "must-never-reach-config-storage"
    reach = {"mode": "selected", "project_ids": ["project-a"], "outside_projects": False}

    rejected_add = client.post(
        "/api/mcp/servers",
        json={"name": "A", "config": {"command": "a", "env": {"SERVICE_TOKEN": literal}}, "key_refs": [], "reach": reach},
    )
    assert rejected_add.status_code == 422
    created = client.post(
        "/api/mcp/servers",
        json={"name": "A", "config": {"command": "a"}, "key_refs": [{"kind": "env", "name": "SERVICE_TOKEN"}], "reach": reach},
    )
    assert created.status_code == 201
    rejected_edit = client.patch(
        f"/api/mcp/servers/{created.json()['id']}",
        json={"config": {"command": "b", "env": {"SERVICE_TOKEN": literal}}, "key_refs": [], "expected_revision": 1},
    )
    assert rejected_edit.status_code == 422

    persisted = "".join(
        row[0] for row in db.execute("SELECT config_json FROM connected_apps").fetchall()
    ) + "".join(row[0] for row in db.execute("SELECT value FROM settings").fetchall())
    assert literal not in persisted


def test_delete_removes_only_a_persons_stable_app_keys(connected_apps_client) -> None:
    client, db, actor = connected_apps_client
    reach = {"mode": "selected", "project_ids": ["project-a"], "outside_projects": False}
    config = {"command": "a", "key_refs": [{"kind": "env", "name": "SERVICE_TOKEN"}]}
    agent_app = create_app("Agent app", config, reach, db=db)
    agent_key = key_entry_names(agent_app["id"], config).pop()
    db.commit()

    deleted_by_agent = client.delete(f"/api/mcp/servers/{agent_app['id']}")

    assert deleted_by_agent.status_code == 200
    assert db.execute("SELECT 1 FROM connected_apps WHERE id=?", (agent_app["id"],)).fetchone() is None
    assert db.execute("SELECT 1 FROM key_store_entries WHERE name=?", (agent_key,)).fetchone() is not None

    person_app = create_app("Person app", config, reach, db=db)
    person_key = key_entry_names(person_app["id"], config).pop()
    db.commit()
    actor.clear()
    actor.update({"id": "user", "token_kind": "session"})

    deleted_by_person = client.delete(f"/api/mcp/servers/{person_app['id']}")

    assert deleted_by_person.status_code == 200
    assert db.execute("SELECT 1 FROM key_store_entries WHERE name=?", (person_key,)).fetchone() is None

    rolled_back_app = create_app("Rollback app", config, reach, db=db)
    rolled_back_key = key_entry_names(rolled_back_app["id"], config).pop()
    db.execute(
        "CREATE TRIGGER prevent_app_delete BEFORE DELETE ON connected_apps "
        f"WHEN OLD.id='{rolled_back_app['id']}' BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    db.commit()

    rejected = client.delete(f"/api/mcp/servers/{rolled_back_app['id']}")

    assert rejected.status_code == 422
    assert db.execute("SELECT 1 FROM connected_apps WHERE id=?", (rolled_back_app["id"],)).fetchone() is not None
    assert db.execute("SELECT 1 FROM key_store_entries WHERE name=?", (rolled_back_key,)).fetchone() is not None


def test_audit_is_durable_and_records_only_actor_app_action_and_reach(connected_apps_client) -> None:
    client, db, actor = connected_apps_client
    actor.clear()
    actor.update({"id": "person", "token_kind": "session"})
    created = client.post(
        "/api/mcp/servers",
        json={
            "name": "Audited", "config": {"command": "a"},
            "key_refs": [{"kind": "header", "name": "SERVICE_TOKEN"}],
            "reach": {"mode": "selected", "project_ids": ["project-a"], "outside_projects": False},
        },
    )
    assert created.status_code == 201
    app_id = created.json()["id"]
    changed = client.put(
        f"/api/mcp/servers/{app_id}/reach",
        json={
            "expected_revision": 1,
            "reach": {"mode": "selected", "project_ids": ["project-b"], "outside_projects": False},
        },
    )
    assert changed.status_code == 200

    rows = db.execute(
        "SELECT actor_kind,actor_id,app_id,action,reach_from_json,reach_to_json "
        "FROM connected_app_audit WHERE app_id=? ORDER BY id",
        (app_id,),
    ).fetchall()

    assert [tuple(row[:4]) for row in rows] == [
        ("person", "person", app_id, "add"),
        ("person", "person", app_id, "reach_change"),
    ]
    assert rows[0][4] is None
    assert rows[0][5] == '{"mode":"selected","outside_projects":false,"project_ids":["project-a"]}'
    assert rows[1][4] == '{"mode":"selected","outside_projects":false,"project_ids":["project-a"]}'
    assert rows[1][5] == '{"mode":"selected","outside_projects":false,"project_ids":["project-b"]}'
    assert "SERVICE_TOKEN" not in "".join(value or "" for row in rows for value in row[4:])


def test_conductor_policy_blocks_agent_approval_routes() -> None:
    from tools.conductor_api_policy import off_limits

    assert off_limits("/api/mcp/servers/app-id/approve", "POST")
    assert off_limits("/api/mcp/servers/app-id/decline", "POST")


def test_reconnect_endpoint_authorizes_and_returns_the_runtime_state(connected_apps_client) -> None:
    client, db, actor = connected_apps_client
    app = create_app(
        "A", {"command": "a", "key_refs": []},
        {"mode": "selected", "project_ids": ["project-a"], "outside_projects": False}, db=db,
    )

    deferred = client.post(f"/api/mcp/servers/{app['id']}/reconnect")

    assert deferred.status_code == 200
    assert deferred.json() == {
        "id": app["id"], "state": "disconnected", "state_since": "t", "tool_count": 0, "failure_class": None,
    }

    actor["thread_id"] = "thread-b"
    refused = client.post(f"/api/mcp/servers/{app['id']}/reconnect")

    assert refused.status_code == 403
