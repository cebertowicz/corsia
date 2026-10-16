"""The sign-in routes: person only (C-R7, C-R9), this-Mac only (C-R8), and what a row shows (C-R1)."""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import connected_app_signin as routes
from api import connected_apps
from api.connected_app_rows import _agent_next_step
from auth_fixtures import OWNER_USER
from connections.app_lifecycle import create_app
from infra.auth import require_user
from infra.database import get_db
from oauth_fake_as import ACCESS, BASE, MCP_URL, REFRESH, FakeAS
from oauth_fixtures import ALL_REACH, SESSION
import tools.mcp_oauth_flow as flow
import tools.mcp_oauth_protocol as protocol
import tools.mcp_oauth_store as store
from tools.mcp_oauth_errors import SignInError

PERSON = {"id": OWNER_USER, "token_kind": "session"}
AGENT = {"id": OWNER_USER, "thread_id": "thread-a", "token_kind": "internal"}
AUTH = {"Authorization": f"Bearer {SESSION}"}


@pytest.fixture
def api(isolated_db, monkeypatch):
    calls: dict[str, list] = {"start": [], "cancel": [], "signout": [], "runtime": []}
    actor = {"who": PERSON}

    async def start(app_id, name, config, *, user_id, session_hash, metadata_url):
        calls["start"].append((app_id, user_id, session_hash, metadata_url))
        return {"authorization_url": "http://127.0.0.1:7999/authorize?x=1", "expires_at": "t", "reopened": False}

    async def cancel(app_id, *, actor):
        calls["cancel"].append((app_id, actor))
        return True

    async def sign_out(app_id, *, actor):
        calls["signout"].append((app_id, actor))
        return {"had_sign_in": True, "provider_told": True, "provider_url": None}

    async def runtime(action, app_id):
        calls["runtime"].append((action, app_id))
        return {"state": "needs_sign_in", "state_since": "t", "tool_count": 0, "failure_class": None}

    monkeypatch.setattr(flow, "start", start)
    monkeypatch.setattr(flow, "cancel", cancel)
    monkeypatch.setattr("tools.mcp_oauth_signout.sign_out", sign_out)
    monkeypatch.setattr("tools.connected_apps_runtime.apply_connected_app_runtime", runtime)
    app_id = create_app("Linear", {"url": MCP_URL, "key_refs": []}, ALL_REACH, db=get_db())["id"]
    local_id = create_app("Local", {"command": "x", "key_refs": []}, ALL_REACH, db=get_db())["id"]
    server = FastAPI()
    server.include_router(connected_apps.connected_apps_router, prefix="/api/mcp")
    server.dependency_overrides[require_user] = lambda: actor["who"]

    def client(host: str = "127.0.0.1", peer: str = "127.0.0.1") -> TestClient:
        return TestClient(server, base_url=f"http://{host}:3001", client=(peer, 5000))

    return client, actor, calls, app_id, local_id


def audit(app_id: str) -> list[str]:
    return [row["action"] for row in get_db().execute("SELECT action FROM connected_app_audit WHERE app_id=? ORDER BY rowid", (app_id,))]


@pytest.mark.parametrize("method,path", [
    ("post", "sign-in"), ("get", "sign-in"), ("delete", "sign-in"), ("post", "sign-out"),
])
def test_an_agent_is_refused_and_recorded_on_every_route(api, method, path):  # C-R7, C-R9
    client, actor, calls, app_id, _ = api
    actor["who"] = AGENT
    response = getattr(client(), method)(f"/api/mcp/servers/{app_id}/{path}")
    assert response.status_code == 403 and "Only the signed-in person" in response.text
    assert not any(calls.values())
    assert len(audit(app_id)) == 1 and audit(app_id)[0].startswith("refused_")


def test_a_person_on_this_mac_starts_a_sign_in_with_their_own_session(api):  # C-R2, C-R8
    client, _, calls, app_id, _ = api
    response = client().post(f"/api/mcp/servers/{app_id}/sign-in", headers=AUTH)
    assert response.status_code == 200 and response.json()["authorization_url"].startswith("http://127.0.0.1")
    from infra.accounts import token_digest

    assert calls["start"] == [(app_id, OWNER_USER, token_digest(SESSION), "")]


def test_the_metadata_setting_reaches_the_flow_only_when_set(api, monkeypatch):  # C-R2
    client, _, calls, app_id, _ = api
    monkeypatch.setattr("infra.config.CLIENT_METADATA_URL", "https://example.test/client.json")
    client().post(f"/api/mcp/servers/{app_id}/sign-in", headers=AUTH)
    assert calls["start"][0][3] == "https://example.test/client.json"


@pytest.mark.parametrize("host,peer,forwarded", [
    ("corsia.example.test", "127.0.0.1", None),
    ("127.0.0.1", "192.168.1.20", None),
    ("127.0.0.1", "127.0.0.1", ("X-Forwarded-For", "203.0.113.9")),
    ("127.0.0.1", "127.0.0.1", ("Forwarded", "for=203.0.113.9")),
    ("127.0.0.1", "127.0.0.1", ("X-Real-IP", "203.0.113.9")),
    ("localhost", "127.0.0.1", ("Tailscale-User-Login", "someone@example.test")),
])
def test_sign_in_is_offered_only_on_the_mac_corsia_runs_on(api, host, peer, forwarded):  # C-R8
    client, _, calls, app_id, _ = api
    headers = {**AUTH, **(dict([forwarded]) if forwarded else {})}
    response = client(host, peer).post(f"/api/mcp/servers/{app_id}/sign-in", headers=headers)
    assert response.status_code == 409 and "Mac CORSIA runs on" in response.text and calls["start"] == []
    assert client(host, peer).get("/api/mcp/servers", headers=headers).json()["sign_in_here"] is False


def test_the_list_says_where_sign_in_works(api):  # C-R8
    client, *_ = api
    assert client().get("/api/mcp/servers", headers=AUTH).json()["sign_in_here"] is True


def test_a_local_app_cannot_be_signed_in_to(api):  # scope
    client, _, calls, _, local_id = api
    assert client().post(f"/api/mcp/servers/{local_id}/sign-in", headers=AUTH).status_code == 422
    assert calls["start"] == []


def test_a_provider_failure_is_a_fixed_class_never_provider_text(api, monkeypatch):  # C-R9
    client, _, _, app_id, _ = api

    async def failing(*args, **kwargs):
        raise SignInError("address_mismatch", "ignored words")

    monkeypatch.setattr(flow, "start", failing)
    response = client().post(f"/api/mcp/servers/{app_id}/sign-in", headers=AUTH)
    assert response.status_code == 502 and response.json() == {"detail": {"failure_class": "address_mismatch"}}


def test_a_person_can_cancel_and_sign_out(api):  # C-R6
    client, _, calls, app_id, _ = api
    assert client().delete(f"/api/mcp/servers/{app_id}/sign-in").json() == {"cancelled": True}
    out = client().post(f"/api/mcp/servers/{app_id}/sign-out").json()
    assert out["id"] == app_id and out["provider_told"] is True and out["state"] == "needs_sign_in"
    assert calls["cancel"] == [(app_id, ("person", OWNER_USER))]
    assert calls["signout"] == [(app_id, ("person", OWNER_USER))] and calls["runtime"] == [("sign_out", app_id)]


def _agent_thread_in(project_id: str) -> None:
    db = get_db()
    db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES (?,?,?,?)", (project_id, project_id, "t", "t"))
    db.execute(
        "INSERT INTO threads(id,user_id,project_id,created_at,updated_at) VALUES (?,?,?,?,?)",
        (AGENT["thread_id"], OWNER_USER, project_id, "t", "t"),
    )
    db.commit()


def _signed_in(app_id: str) -> store.Grant:
    grant = store.Grant(
        app_id=app_id, signed_in_by=OWNER_USER, server_url=MCP_URL, issuer=BASE, resource=MCP_URL,
        token_endpoint=f"{BASE}/token", client_id="dcr-client-1", access_token=ACCESS.format(n=7),
        expires_at=time.time() + 3600, revocation_endpoint=f"{BASE}/revoke", refresh_token=REFRESH.format(n=7),
    )
    store.replace(grant)
    return grant


@pytest.fixture
def provider(api, monkeypatch):
    fake = FakeAS()
    monkeypatch.setattr(protocol, "new_client", fake.client)

    async def runtime_noop(_action, _app_id):
        return {"state": "disconnected", "state_since": "t", "tool_count": 0, "failure_class": None}

    monkeypatch.setattr(connected_apps, "_run_app_runtime", runtime_noop)
    return fake


@pytest.mark.parametrize("who,audited", [(PERSON, ("person", OWNER_USER)), (AGENT, ("agent", "thread-a"))])
def test_remove_deletes_the_app_and_its_sign_in_then_revokes_it(api, provider, who, audited):  # Governance 3, 5
    client, actor, _, app_id, _ = api
    _agent_thread_in("project-a")
    grant = _signed_in(app_id)
    actor["who"] = who
    assert client().delete(f"/api/mcp/servers/{app_id}").status_code == 200
    assert store.load(app_id) is None and provider.revoked == [grant.refresh_token]
    row = get_db().execute(
        "SELECT actor_kind, actor_id FROM connected_app_audit WHERE app_id=? AND action='remove_revoked'", (app_id,),
    ).fetchone()
    assert row is not None and (row["actor_kind"], row["actor_id"]) == audited


def test_a_failed_revoke_on_remove_is_audited_and_the_app_stays_removed(api, provider):  # Governance 3
    client, _, _, app_id, _ = api
    _signed_in(app_id)
    provider.revoke_mode = "fail"
    assert client().delete(f"/api/mcp/servers/{app_id}").status_code == 200
    assert get_db().execute("SELECT 1 FROM connected_apps WHERE id=?", (app_id,)).fetchone() is None
    assert store.load(app_id) is None and audit(app_id)[-1] == f"remove_revoke_failed:refused:{BASE}"


def test_removing_a_missing_app_ends_and_revokes_nothing(api, provider):  # Governance 3
    client, _, _, app_id, _ = api
    grant = _signed_in(app_id)
    assert client().delete("/api/mcp/servers/no-such-app").status_code == 404
    assert provider.revoked == [] and store.load(app_id).access_token == grant.access_token


def test_a_callback_host_the_listener_does_not_answer_is_refused_when_saved(api):  # C-R9
    client, *_ = api
    for host in ("[::1]", "::1", "0.0.0.0"):
        body = {"name": "Hosted", "config": {"url": MCP_URL, "oauth_callback": {"host": host}}}
        response = client().post("/api/mcp/servers", json=body, headers=AUTH)
        assert response.status_code == 422 and "127.0.0.1 or localhost" in response.text


def test_an_agent_cannot_sign_out_an_app_its_project_cannot_reach(api, monkeypatch):  # C-R6
    client, actor, calls, _, _ = api
    _agent_thread_in("project-a")
    get_db().execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES ('project-b','project-b','t','t')")
    get_db().commit()
    actor["who"] = AGENT
    hidden = create_app(
        "Hidden", {"url": MCP_URL, "key_refs": []},
        {"mode": "selected", "project_ids": ["project-b"], "outside_projects": False}, db=get_db(),
    )["id"]
    assert client().delete(f"/api/mcp/servers/{hidden}").status_code == 403
    assert calls["signout"] == []


def test_a_row_shows_sign_in_facts_without_a_link_or_token(api):  # C-R1
    client, actor, _, app_id, local_id = api
    rows = {row["id"]: row for row in client().get("/api/mcp/servers", headers=AUTH).json()["servers"]}
    assert rows[app_id]["sign_in"] == {"status": "idle", "signed_in": False}
    assert "sign_in" not in rows[local_id]
    assert "authorization_url" not in str(rows)


def test_a_signed_in_app_has_one_sign_in_whoever_signed_in(api):  # finding 1: no two-grant dead end
    client, _, _, app_id, _ = api
    first = _signed_in(app_id)
    store.replace(store.Grant(**{**first.__dict__, "signed_in_by": "someone-else", "access_token": ACCESS.format(n=8)}))
    names = [row["name"] for row in get_db().execute("SELECT name FROM key_store_entries WHERE name LIKE 'mcp-oauth-%'")]
    assert names == [store.entry_name(app_id)]
    row = {row["id"]: row for row in client().get("/api/mcp/servers", headers=AUTH).json()["servers"]}[app_id]
    assert row["sign_in"]["signed_in"] is True and "someone-else" not in str(row)
    assert ACCESS.format(n=8) not in str(row)


def test_agents_are_told_to_ask_the_person_to_sign_in():  # C-R7
    text = _agent_next_step("Linear", "needs_sign_in", None, [], on_here=True)
    assert "Ask the person to press Sign in" in text and "try to sign in yourself" in text


def test_same_mac_check_unit():  # C-R8
    class Req:
        def __init__(self, host, peer, headers=()):
            self.headers = {"host": host, **{name.lower(): value for name, value in headers}}
            self.client = type("C", (), {"host": peer})()

    assert routes.sign_in_here(Req("127.0.0.1:3001", "127.0.0.1"))
    assert routes.sign_in_here(Req("localhost:3001", "::1"))
    assert routes.sign_in_here(Req("[::1]:3001", "::1"))
    assert not routes.sign_in_here(Req("localhost.evil.example:3001", "127.0.0.1"))
    assert not routes.sign_in_here(Req("127.0.0.1:3001", "10.0.0.5"))
    assert not routes.sign_in_here(Req("127.0.0.1:3001", "127.0.0.1", [("X-Real-IP", "203.0.113.9")]))
