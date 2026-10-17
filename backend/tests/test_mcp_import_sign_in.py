"""A sign-in import keeps the one approval gate: nothing reaches the app before a person approves it.

Journey (A-R16, C-R1, C-R7): an agent imports a hosted app that needs a sign-in; it waits for
approval and calls nothing; the person's approval connects once, which the app answers with a
sign-in challenge; the person signs in and the app works. The fake provider is reached through
``httpx.ASGITransport`` on the real connect path, so every call it receives is counted.
"""
from __future__ import annotations

import asyncio
import json
import logging
import types

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth_fixtures import OWNER_USER
from infra import state
from infra.auth import require_user, require_user_header_only
from infra.database import get_db
from oauth_fake_as import BASE, MCP_URL
from oauth_fixtures import SESSION, browse, env  # noqa: F401
from tests.mcp_import_fixtures import seed_project
from tools import mcp_http_transport, mcp_import, mcp_runtime
import tools.mcp_oauth_flow as flow
from tools.registry import ToolRegistry

PERSON = {"id": OWNER_USER, "token_kind": "session"}
AGENT = {"id": OWNER_USER, "thread_id": "thread-a", "token_kind": "internal"}
AUTH = {"Authorization": f"Bearer {SESSION}"}
#: The real post-sign-in connect, which the shared ``env`` fixture replaces with a recorder.
CONNECT_AFTER_SIGN_IN = flow._connect_in_background


def _claude_json(home, servers: dict, *, folder=None) -> None:
    document = {"projects": {str(folder): {"mcpServers": servers}}} if folder else {"mcpServers": servers}
    (home / ".claude.json").write_text(json.dumps(document), encoding="utf-8")


@pytest.fixture
def journey(env, tmp_path, monkeypatch):  # noqa: F811
    """The import and Connected Apps routes, a fresh runtime, and every MCP request sent to the fake app."""
    from api.mcp import router
    from connections.app_lifecycle import delete_app

    delete_app(env.app_id, remove_keys=True, db=get_db())  # only the imported app is configured
    monkeypatch.setattr(mcp_import, "_home", lambda: tmp_path)
    for name, value in (
        ("_stopped", False), ("_lock", asyncio.Lock()), ("_registry", ToolRegistry()), ("_app_stacks", {}),
        ("_app_tools", {}), ("_app_servers", {}), ("_app_configs", {}), ("_app_states", {}), ("_reconnect_at", {}),
    ):
        monkeypatch.setattr(mcp_runtime, name, value)
    monkeypatch.setattr(state, "_agent", None)
    real_client = httpx.AsyncClient

    def to_fake_app(**options):
        return real_client(transport=httpx.ASGITransport(app=env.fake.app), **options)

    routed = types.SimpleNamespace(**{name: getattr(httpx, name) for name in dir(httpx) if not name.startswith("_")})
    routed.AsyncClient = to_fake_app
    monkeypatch.setattr(mcp_http_transport, "httpx", routed)
    _claude_json(tmp_path, {"Tracker": {"type": "http", "url": MCP_URL, "oauth": {}, "headers": {"Authorization": "old-token"}}})
    actor = {"who": AGENT}
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user_header_only] = lambda: actor["who"]
    app.dependency_overrides[require_user] = lambda: actor["who"]
    with TestClient(app, base_url="http://127.0.0.1:3001", client=("127.0.0.1", 5000)) as client:
        client.actor = actor
        yield client
        client.portal.call(flow.stop_all)
        client.portal.call(mcp_runtime.stop_mcp_runtime)


def _import(client, name: str = "Tracker") -> dict:
    client.actor["who"] = AGENT
    response = client.post("/api/mcp/import", json={"servers": [{"source": "claude_code", "name": name}]})
    assert response.status_code == 200
    [row] = response.json()["rows"]
    return row


def _as_person(client) -> None:
    client.actor["who"] = PERSON


def _listed(client, app_id: str) -> dict:
    _as_person(client)
    [row] = [row for row in client.get("/api/mcp/servers").json()["servers"] if row["id"] == app_id]
    return row


async def test_a_sign_in_import_waits_for_approval_and_calls_nothing(journey, env):  # noqa: F811
    row = _import(journey)

    assert row == {"id": row["id"], "name": "Tracker", "state": "awaiting_approval"}
    from connections.app_reach import get_app

    stored = get_app(row["id"], db=get_db())
    assert stored["state"] == "awaiting_approval"
    assert stored["config"] == {"url": MCP_URL, "type": "http", "transport": "http", "key_refs": []} or (
        stored["config"]["url"] == MCP_URL and stored["config"]["key_refs"] == [] and "headers" not in stored["config"]
    )
    assert get_db().execute("SELECT count(*) FROM key_store_entries").fetchone()[0] == 0
    started = journey.portal.call(mcp_runtime.start_connected_app_runtime)
    assert row["id"] not in started
    _as_person(journey)
    assert journey.post(f"/api/mcp/servers/{row['id']}/sign-in", headers=AUTH).status_code == 422
    assert journey.post(f"/api/mcp/servers/{row['id']}/reconnect").status_code >= 400
    assert _listed(journey, row["id"])["state"] == "awaiting_approval"
    assert env.fake.mcp_calls == 0 and env.fake.registrations == []


async def test_approve_connects_once_to_a_sign_in_then_the_persons_sign_in_makes_it_work(journey, env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(flow, "_connect_in_background", CONNECT_AFTER_SIGN_IN)
    app_id = _import(journey)["id"]
    _as_person(journey)

    approved = journey.post(f"/api/mcp/servers/{app_id}/approve", json={"expected_revision": 1})

    assert approved.status_code == 200 and approved.json()["state"] == "needs_sign_in"
    assert env.fake.mcp_calls >= 1 and env.fake.mcp_hosts_with_auth == []  # the first connect sent no token
    assert _listed(journey, app_id)["state"] == "needs_sign_in"

    journey.actor["who"] = AGENT
    assert journey.post(f"/api/mcp/servers/{app_id}/sign-in", headers=AUTH).status_code == 403  # C-R7
    _as_person(journey)
    started = journey.post(f"/api/mcp/servers/{app_id}/sign-in", headers=AUTH)
    assert started.status_code == 200 and started.json()["authorization_url"].startswith(f"{BASE}/authorize")

    await browse(env.fake.consent(started.json()["authorization_url"]))
    journey.portal.call(lambda: asyncio.gather(*flow._background))

    row = _listed(journey, app_id)
    assert (row["state"], row["tool_count"], row["sign_in"]["signed_in"]) == ("working", 1, True)
    assert mcp_runtime.connected_app_state(app_id)["state"] == "working"


async def test_an_edit_before_approval_reaches_nothing_and_an_agent_edit_after_waits_again(journey, env):  # noqa: F811
    row = _import(journey)
    _as_person(journey)
    edit = {"config": {"url": f"{BASE}/other"}, "expected_revision": 1}

    assert journey.patch(f"/api/mcp/servers/{row['id']}", json=edit).status_code == 409
    assert env.fake.mcp_calls == 0
    assert journey.post(f"/api/mcp/servers/{row['id']}/approve", json={"expected_revision": 1}).json()["state"] == "needs_sign_in"
    calls = env.fake.mcp_calls

    from connections.app_lifecycle import propose_config
    from connections.app_reach import get_app

    proposed = propose_config(row["id"], {"url": f"{BASE}/other", "key_refs": []}, actor_thread_id="thread-a", expected_revision=2, db=get_db())
    journey.portal.call(mcp_runtime.start_connected_app_runtime)

    assert proposed["state"] == "awaiting_approval" and get_app(row["id"], db=get_db())["config"]["url"] == MCP_URL
    assert env.fake.mcp_calls == calls


async def test_a_sign_in_import_from_a_folder_no_project_owns_gets_the_place_step(journey, env, tmp_path):  # noqa: F811
    folder = tmp_path / "not-a-project"
    owned = tmp_path / "owned"
    owned.mkdir()
    seed_project(get_db(), "q4", owned)
    _claude_json(tmp_path, {"Tracker": {"type": "http", "url": MCP_URL, "oauth": {}}}, folder=folder)

    row = _import(journey)

    assert row == {
        "id": row["id"], "name": "Tracker", "state": "awaiting_approval", "folder": "not-a-project",
        "message": mcp_import.IMPORT_FOLDER_WAITING_FOR_PLACE.format(App="Tracker"),
    }
    listed = _listed(journey, row["id"])
    assert listed["folder"] == "not-a-project"
    assert listed["reach"] == {"mode": "selected", "project_ids": [], "outside_projects": False}
    assert journey.post(f"/api/mcp/servers/{row['id']}/approve", json={"expected_revision": 1}).status_code >= 400
    chosen = {"mode": "selected", "project_ids": ["q4"], "outside_projects": False}
    approved = journey.post(f"/api/mcp/servers/{row['id']}/approve", json={"expected_revision": 1, "reach": chosen})
    assert approved.status_code == 200 and approved.json()["state"] == "needs_sign_in"


async def test_the_sign_in_route_refuses_a_waiting_or_expired_request(journey, env):  # noqa: F811
    row = _import(journey)
    _as_person(journey)
    assert journey.post(f"/api/mcp/servers/{row['id']}/sign-in", headers=AUTH).status_code == 422
    get_db().execute("UPDATE connected_apps SET state_since='2000-01-01T00:00:00' WHERE id=?", (row["id"],))
    get_db().commit()

    assert _listed(journey, row["id"])["state"] == "expired"
    assert journey.post(f"/api/mcp/servers/{row['id']}/sign-in", headers=AUTH).status_code == 422
    assert env.fake.registrations == [] and flow._flows == {}


async def test_every_saved_write_reaches_the_running_app_at_once(isolated_db, monkeypatch):
    from connections.app_lifecycle import create_app, replace_config
    from tools.connected_apps_runtime import restart_after_saved_write

    for name, value in (
        ("_stopped", False), ("_lock", asyncio.Lock()), ("_registry", ToolRegistry()), ("_app_stacks", {}),
        ("_app_tools", {}), ("_app_servers", {}), ("_app_configs", {}), ("_app_states", {}), ("_reconnect_at", {}),
    ):
        monkeypatch.setattr(mcp_runtime, name, value)
    monkeypatch.setattr(state, "_agent", None)
    seen: list[object] = []

    async def connects(configs, _registry, _stack):
        [(_name, config)] = configs.items()
        seen.append(config.get("connect_timeout"))
        return {}

    monkeypatch.setattr(mcp_runtime, "connect_mcp_servers", connects)
    app_id = create_app("Notes", {"url": MCP_URL, "key_refs": [], "connect_timeout": 5}, {"mode": "all", "project_ids": [], "outside_projects": False}, db=get_db())["id"]
    await restart_after_saved_write(app_id)
    replace_config(app_id, {"url": MCP_URL, "key_refs": [], "connect_timeout": 7}, expected_revision=1, db=get_db())

    second = await restart_after_saved_write(app_id)

    assert seen == [5, 7] and second["state"] == "working"


async def test_a_saved_write_that_cannot_restart_is_logged_with_its_app_and_class(isolated_db, monkeypatch, caplog):
    from tools.connected_apps_runtime import restart_after_saved_write

    monkeypatch.setattr(mcp_runtime, "_lock", asyncio.Lock())
    monkeypatch.setattr(mcp_runtime, "_stopped", False)
    caplog.set_level(logging.WARNING)

    result = await restart_after_saved_write("gone-app")

    assert result["state"] == "disconnected"
    assert "app_id=gone-app" in caplog.text and "AppNotFoundError" in caplog.text
