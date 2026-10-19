"""Local-only enforcement: the server fact, the catalog overlay, send admission and the routes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from auth_fixtures import seed_account
from fastapi import FastAPI
from fastapi.testclient import TestClient
from vendor_account_fixtures import accounts  # noqa: F401

SESSION = "locality-session-token-01"
API = "locality-api-token-01"
CLOUD_OFF, CLOUD_ON = "actor-project", "destination-project"
LOCAL, CLOUD = "omlx@loop/m", "openai/gpt-x"
LOOPBACK, LAN = "127.0.0.1", "192.168.1.2"


def _server(db, server_id, host, *, kind="managed", private=False):
    db.execute(
        "INSERT INTO network_servers(id,provider,kind,instance_id,instance_name,port,status,metadata_json,"
        "last_seen_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (server_id, "external" if kind == "external" else "omlx", kind, server_id, server_id, 8000, "online",
         '{"private": true}' if private else "{}", "t", "t", "t"),
    )
    db.execute(
        "INSERT INTO network_server_endpoints(id,server_id,host,kind,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        (f"ep-{server_id}", server_id, host, "local" if host == LOOPBACK else "lan", "t", "t"),
    )
    db.commit()


def _model(db, model_id, *, provider, backend):
    db.execute(
        "INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,backend,"
        "created_at,updated_at) VALUES (?,?,?,?,?,'native',?,'t','t')",
        (model_id, model_id, model_id, provider, model_id.rpartition("/")[2], backend),
    )
    db.commit()


@pytest.fixture
def world(accounts):  # noqa: F811
    from llm.locality import set_project_cloud_models_off

    db = accounts[0]
    _server(db, "loop", LOOPBACK)
    _model(db, LOCAL, provider="omlx", backend="network")
    _model(db, CLOUD, provider="openai", backend="api")
    db.execute("INSERT INTO threads(id,user_id,model,project_id,created_at,updated_at) "
               "VALUES ('noproject','user',?,NULL,'t','t')", (CLOUD,))
    db.commit()
    set_project_cloud_models_off(CLOUD_OFF, cloud_models_off=True, db=db)
    return db


def _row(**fields):
    return {"status": "ready", "status_reason": None, "is_local": False, "litellm_model": fields["id"], **fields}


def test_project_accounts_overlays_cloud_rows_only_in_a_cloud_off_project(world):
    from llm.account_catalog import project_accounts

    rows = [
        _row(id="codex/test", connection="codex", runnable={"status": "ready", "status_reason": None}),
        _row(id="claude-code/test", connection="claude-code", runnable={"status": "ready", "status_reason": None}),
        _row(id=CLOUD, connection="openai"),
        _row(id=LOCAL, connection="omlx", is_local=True),
    ]
    off = {r["id"]: r for r in project_accounts(rows, project_id=CLOUD_OFF, db=world)}
    for cloud in ("codex/test", "claude-code/test", CLOUD):
        assert (off[cloud]["status"], off[cloud]["status_reason"]) == ("unavailable", "project_cloud_off")
    for harness in ("codex/test", "claude-code/test"):
        assert off[harness]["runnable"] == {"status": "unavailable", "status_reason": "project_cloud_off"}
    assert "runnable" not in off[CLOUD]
    assert (off[LOCAL]["status"], off[LOCAL]["status_reason"]) == ("ready", None)

    on = {r["id"]: r for r in project_accounts(rows, project_id=CLOUD_ON, db=world)}
    assert all(r["status_reason"] != "project_cloud_off" for r in on.values())
    assert on[CLOUD]["status"] == "ready" and on[LOCAL]["status"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["codex/test", CLOUD])
async def test_cloud_off_thread_is_refused_on_a_cloud_row(world, model):
    from infra import send_target
    from infra.unavailable_model_recovery import resolve_or_hold

    for launch in (
        lambda: send_target.resolve_target(model, thread_id="actor"),
        lambda: resolve_or_hold(model, SimpleNamespace(thread_id="actor")),
    ):
        with pytest.raises(send_target.TargetUnavailable) as refused:
            await launch()
        assert "project_cloud_off" in refused.value.detail


@pytest.mark.asyncio
async def test_local_row_and_unscoped_threads_still_resolve(world):
    from infra import send_target
    from infra.unavailable_model_recovery import resolve_or_hold

    batch = SimpleNamespace(thread_id="actor")
    assert (await send_target.resolve_target(LOCAL, thread_id="actor")).catalog_id == LOCAL
    assert (await resolve_or_hold(LOCAL, batch)).catalog_id == LOCAL
    for thread in ("noproject", "destination", None):
        assert (await send_target.resolve_target(CLOUD, thread_id=thread)).catalog_id == CLOUD
    assert (await resolve_or_hold(CLOUD, SimpleNamespace(thread_id="noproject"))).catalog_id == CLOUD


@pytest.mark.parametrize(
    ("server_id", "host", "kind", "private", "on_this_mac", "row_local"),
    [
        ("loop", LOOPBACK, "managed", False, True, True),
        ("lan", LAN, "managed", False, False, False),
        ("lan", LAN, "managed", True, False, True),
        ("ext", LOOPBACK, "external", False, False, False),
        ("ext", LOOPBACK, "external", True, False, True),
    ],
)
def test_server_and_its_rows_share_one_locality_rule(world, server_id, host, kind, private, on_this_mac, row_local):
    from llm.locality import locality_refusal, server_on_this_mac

    if server_id != "loop":
        _server(world, server_id, host, kind=kind, private=private)
    server = dict(world.execute("SELECT * FROM network_servers WHERE id = ?", (server_id,)).fetchone())
    assert server_on_this_mac(server) is on_this_mac
    prefix = "external" if kind == "external" else "omlx"
    row = {"litellm_model": f"{prefix}@{server_id}/m", "backend": "network", "provider": prefix, "harness": "native"}
    refusal = locality_refusal(row, thread_local_only=False, project_id=CLOUD_OFF, db=world)
    assert (refusal is None) is row_local
    assert refusal in (None, "project_cloud_off")


def test_a_server_with_a_non_loopback_endpoint_or_none_is_not_on_this_mac(world):
    from llm.locality import server_on_this_mac

    server = dict(world.execute("SELECT * FROM network_servers WHERE id = 'loop'").fetchone())
    assert server_on_this_mac(server, []) is False
    both = [{"host": LOOPBACK}, {"host": LAN}]
    assert server_on_this_mac(server, both) is False


@pytest.fixture
def client(world):
    seed_account("person", SESSION, kind="session")
    seed_account("agent", API, kind="api")
    from api.network_servers import router as servers
    from api.provider_policy import router as policy

    app = FastAPI()
    app.include_router(policy)
    app.include_router(servers)
    return TestClient(app)


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def test_cloud_models_off_round_trips_and_rejects_a_non_bool(client):
    path = f"/api/projects/{CLOUD_ON}/cloud-models-off"
    assert client.get(path, headers=_h(SESSION)).json() == {"project_id": CLOUD_ON, "cloud_models_off": False}
    put = client.put(path, json={"cloud_models_off": True}, headers=_h(SESSION))
    assert (put.status_code, put.json()) == (200, {"project_id": CLOUD_ON, "cloud_models_off": True})
    assert client.get(path, headers=_h(SESSION)).json()["cloud_models_off"] is True
    for bad in ("true", 1, None):
        assert client.put(path, json={"cloud_models_off": bad}, headers=_h(SESSION)).status_code == 422
    assert client.get(path, headers=_h(SESSION)).json()["cloud_models_off"] is True


def test_cloud_models_off_on_an_unknown_project_uses_the_policy_error_shape(client):
    refused = client.put("/api/projects/nope/cloud-models-off", json={"cloud_models_off": True}, headers=_h(SESSION))
    existing = client.put("/api/projects/nope/provider-policy/use-workspace",
                          json={"expected_revision": 0, "use_workspace": False}, headers=_h(SESSION))
    assert (refused.status_code, refused.json()) == (409, {"detail": "project does not exist"})
    assert (existing.status_code, existing.json()) == (refused.status_code, refused.json())


def test_private_mark_shows_on_the_server_list_and_unknown_server_404s(client):
    def view():
        servers = client.get("/api/settings/network-servers", headers=_h(SESSION)).json()["servers"]
        return next(s for s in servers if s["id"] == "loop")

    assert (view()["private"], view()["on_this_mac"]) == (False, True)
    put = client.put("/api/settings/network-servers/loop/private", json={"private": True}, headers=_h(SESSION))
    assert (put.status_code, put.json()) == (200, {"server_id": "loop", "private": True})
    assert (view()["private"], view()["on_this_mac"]) == (True, True)
    assert client.put("/api/settings/network-servers/loop/private", json={"private": "yes"},
                      headers=_h(SESSION)).status_code == 422
    missing = client.put("/api/settings/network-servers/nope/private", json={"private": True}, headers=_h(SESSION))
    assert missing.status_code == 404


def test_the_server_view_reports_on_this_mac_beside_private(world, client):
    _server(world, "lan", LAN, private=True)
    _server(world, "ext", LOOPBACK, kind="external")
    servers = {s["id"]: s for s in client.get("/api/settings/network-servers", headers=_h(SESSION)).json()["servers"]}
    assert [(servers[i]["on_this_mac"], servers[i]["private"]) for i in ("loop", "lan", "ext")] == [
        (True, False), (False, True), (False, False)]


def test_an_agent_cannot_write_either_setting(client, world):
    from llm.locality import project_cloud_models_off

    policy = client.put(f"/api/projects/{CLOUD_ON}/cloud-models-off", json={"cloud_models_off": True}, headers=_h(API))
    private = client.put("/api/settings/network-servers/loop/private", json={"private": True}, headers=_h(API))
    assert (policy.status_code, private.status_code) == (403, 403)
    assert project_cloud_models_off(CLOUD_ON, db=world) is False
    row = world.execute("SELECT metadata_json FROM network_servers WHERE id = 'loop'").fetchone()
    assert "private" not in row["metadata_json"]


def test_catalog_rows_carry_the_server_fact_as_is_local(world):
    from llm.model_status import get_model_status_rows

    _server(world, "lan", LAN)
    _model(world, "omlx@lan/n", provider="omlx", backend="network")
    rows = {r["id"]: r for r in get_model_status_rows()}
    assert (rows[LOCAL]["is_local"], rows["omlx@lan/n"]["is_local"]) == (True, False)
    world.execute("UPDATE network_servers SET metadata_json = '{\"private\": true}' WHERE id = 'lan'")
    world.commit()
    rows = {r["id"]: r for r in get_model_status_rows()}
    assert rows["omlx@lan/n"]["is_local"] is True
