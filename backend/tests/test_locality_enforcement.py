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
def world(accounts, monkeypatch):  # noqa: F811
    from llm.locality import set_project_cloud_models_off
    from unavailable_model_support import mark_model_loaded

    db = accounts[0]
    _server(db, "loop", LOOPBACK)
    _model(db, LOCAL, provider="omlx", backend="network")
    _model(db, CLOUD, provider="openai", backend="api")
    mark_model_loaded(monkeypatch, LOCAL)
    db.execute("INSERT INTO threads(id,user_id,model,project_id,created_at,updated_at) "
               "VALUES ('noproject','user',?,NULL,'t','t')", (CLOUD,))
    db.commit()
    set_project_cloud_models_off(CLOUD_OFF, cloud_models_off=True, db=db)
    return db


def _row(**fields):
    return {"status": "ready", "status_reason": None, "is_local": False, "litellm_model": fields["id"], **fields}


@pytest.mark.parametrize(("thread_local_only", "cloud_off", "expected"), [
    (False, False, None),
    (False, True, "project_cloud_off"),
    (True, False, "thread_local_only"),
    (True, True, "thread_local_only"),
])
def test_locality_cause_prioritizes_the_thread(
    thread_local_only: bool, cloud_off: bool, expected: str | None,
) -> None:
    from llm.locality import locality_cause

    assert locality_cause(thread_local_only=thread_local_only, cloud_off=cloud_off) == expected


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


def test_project_accounts_marks_cloud_rows_for_a_local_only_thread_alone(world):
    from llm.account_catalog import project_accounts

    for thread_id, local_only, project in (("lo", 1, CLOUD_ON), ("plain", 0, CLOUD_ON), ("both", 1, CLOUD_OFF)):
        world.execute("INSERT INTO threads(id,user_id,model,project_id,local_only,created_at,updated_at) "
                      "VALUES (?,?,?,?,?,'t','t')", (thread_id, "user", LOCAL, project, local_only))
    world.commit()
    rows = [
        _row(id="codex/test", connection="codex", runnable={"status": "ready", "status_reason": None}),
        _row(id=CLOUD, connection="openai"),
        _row(id=LOCAL, connection="omlx", is_local=True),
    ]

    def read(thread_id):
        return {r["id"]: r for r in project_accounts(rows, thread_id=thread_id, db=world)}

    local_only = read("lo")
    for cloud in ("codex/test", CLOUD):
        assert (local_only[cloud]["status"], local_only[cloud]["status_reason"]) == ("unavailable", "thread_local_only")
    assert local_only["codex/test"]["runnable"] == {"status": "unavailable", "status_reason": "thread_local_only"}
    assert (local_only[LOCAL]["status"], local_only[LOCAL]["status_reason"]) == ("ready", None)

    ordinary = read("plain")
    assert [(r["status"], r["status_reason"]) for r in ordinary.values()] == [("ready", None)] * 3
    assert read("both")[CLOUD]["status_reason"] == "thread_local_only"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["codex/test", CLOUD])
async def test_cloud_off_thread_resolves_to_an_unavailable_target_admission_holds(world, model):
    from infra import send_target, unavailable_model_recovery

    target = await send_target.resolve_target(model, thread_id="actor")

    assert (target.available, target.availability_reason) == (False, "project_cloud_off")
    assert send_target.classify_target_admission(target) == "wait"
    assert all(unavailable_model_recovery.model_admission(model, db=world, thread_id="actor", person_send=person) == "wait"
               for person in (True, False))


@pytest.mark.asyncio
async def test_local_row_and_unscoped_threads_still_resolve(world):
    from infra import send_target
    from infra.unavailable_model_recovery import resolve_or_hold

    batch = SimpleNamespace(thread_id="actor", items=())
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


def test_cloud_models_off_on_an_unknown_project_uses_the_policy_error_shape(client, monkeypatch):
    effects = []
    monkeypatch.setattr("api.provider_policy.notify_models_changed", lambda: effects.append("notify"))
    monkeypatch.setattr("api.provider_policy.emit_product_event", lambda *a, **k: effects.append("event"))
    refused = client.put("/api/projects/nope/cloud-models-off", json={"cloud_models_off": True}, headers=_h(SESSION))
    existing = client.put("/api/projects/nope/provider-policy/use-workspace",
                          json={"expected_revision": 0, "use_workspace": False}, headers=_h(SESSION))
    assert (refused.status_code, refused.json()) == (404, {"detail": "project does not exist", "code": "project_not_found"})
    assert (existing.status_code, existing.json()) == (refused.status_code, refused.json())
    assert effects == []


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


def test_an_agent_cannot_write_either_setting(client, world, caplog):
    from api.locality_policy_guard import PERSON_ONLY
    from llm.locality import project_cloud_models_off

    caplog.set_level("INFO", logger="corsia.locality_audit")
    policy = client.put(f"/api/projects/{CLOUD_ON}/cloud-models-off", json={"cloud_models_off": True}, headers=_h(API))
    private = client.put("/api/settings/network-servers/loop/private", json={"private": True}, headers=_h(API))
    assert (policy.status_code, private.status_code) == (403, 403)
    assert policy.json() == private.json() == {"detail": PERSON_ONLY}
    refused = [r.getMessage() for r in caplog.records if r.name == "corsia.locality_audit"]
    assert refused == [
        f"locality policy write refused: subject=project_cloud_models_off name={CLOUD_ON} token_kind=api",
        "locality policy write refused: subject=server_private name=loop token_kind=api",
    ]
    assert project_cloud_models_off(CLOUD_ON, db=world) is False
    row = world.execute("SELECT metadata_json FROM network_servers WHERE id = 'loop'").fetchone()
    assert "private" not in row["metadata_json"]


def test_turn_time_local_only_refuses_a_catalog_row_after_its_server_stops_being_local(world):
    from infra.local_only import LocalityRefused, require_local_only_model

    require_local_only_model(LOCAL, thread_local_only=True, db=world)
    world.execute("UPDATE network_server_endpoints SET host=? WHERE server_id='loop'", (LAN,))
    world.commit()

    with pytest.raises(LocalityRefused) as refused:
        require_local_only_model(LOCAL, thread_local_only=True, db=world)

    assert str(refused.value).startswith("local_only_requires_local_model:")


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


def test_both_person_writes_notify_and_emit(client, world, monkeypatch):
    """The models-changed notice is what releases a locality hold; no per-thread refresh runs."""
    seen = {"notified": [], "events": []}
    monkeypatch.setattr("api.provider_policy.notify_models_changed", lambda: seen["notified"].append("policy"))
    monkeypatch.setattr("api.network_servers.notify_models_changed", lambda: seen["notified"].append("server"))
    monkeypatch.setattr("api.provider_policy.emit_product_event", lambda *a, **k: seen["events"].append((a, k)))
    monkeypatch.setattr("api.network_servers.emit_product_event", lambda *a, **k: seen["events"].append((a, k)))

    policy = client.put(f"/api/projects/{CLOUD_ON}/cloud-models-off", json={"cloud_models_off": True}, headers=_h(SESSION))
    private = client.put("/api/settings/network-servers/loop/private", json={"private": True}, headers=_h(SESSION))

    assert (policy.status_code, private.status_code) == (200, 200)
    assert seen["notified"] == ["policy", "server"]
    assert seen["events"] == [
        (("project", "cloud_models_off.changed"), {"properties": {"project_id": CLOUD_ON, "cloud_models_off": True}}),
        (("server", "private.changed"), {"properties": {"server_id": "loop", "private": True}}),
    ]


def test_a_rejected_write_notifies_nothing(client, monkeypatch):
    notified: list = []
    monkeypatch.setattr("api.provider_policy.notify_models_changed", lambda: notified.append(1))
    monkeypatch.setattr("api.network_servers.notify_models_changed", lambda: notified.append(1))

    client.put(f"/api/projects/{CLOUD_ON}/cloud-models-off", json={"cloud_models_off": True}, headers=_h(API))
    client.put("/api/settings/network-servers/loop/private", json={"private": True}, headers=_h(API))
    client.put("/api/settings/network-servers/nope/private", json={"private": True}, headers=_h(SESSION))

    assert notified == []


@pytest.mark.parametrize("stored", ['{"private": true}', '{"private": "false"}', '{"private": 1}', '{"private": "true"}', "{}", "nonsense"])
def test_every_locality_reader_agrees_on_a_server_whatever_its_stored_mark(world, stored):
    from api.network_servers import _public_server_view
    from llm.locality import locality_refusal, server_locality
    from llm.model_status import _server_locality

    _server(world, "lan", LAN)
    world.execute("UPDATE network_servers SET metadata_json = ? WHERE id = 'lan'", (stored,))
    world.commit()
    server = dict(world.execute("SELECT * FROM network_servers WHERE id = 'lan'").fetchone())
    endpoints = [dict(e) for e in world.execute("SELECT * FROM network_server_endpoints WHERE server_id = 'lan'")]
    expected = stored == '{"private": true}'

    facts = server_locality(server, endpoints)
    private, local = _server_locality([server])
    view = _public_server_view(dict(server), {})
    refusal = locality_refusal(
        {"litellm_model": "omlx@lan/m", "backend": "network", "provider": "omlx", "harness": "native"},
        thread_local_only=False, project_id=CLOUD_OFF, db=world,
    )

    assert facts.private is expected and facts.on_this_mac is False
    assert (("lan" in private), ("lan" in local)) == (expected, False)
    assert (view["private"], view["on_this_mac"]) == (expected, False)
    assert (refusal is None) is expected


def test_the_public_server_view_reads_nothing_when_the_route_loaded_the_endpoints(world, monkeypatch):
    from api.network_servers import _public_server_view

    server = dict(world.execute("SELECT * FROM network_servers WHERE id = 'loop'").fetchone())
    endpoints = [dict(e) for e in world.execute("SELECT * FROM network_server_endpoints WHERE server_id = 'loop'")]

    def forbidden(*_a, **_k):
        raise AssertionError("the view read the database")

    monkeypatch.setattr("llm.network_endpoints.get_endpoints", forbidden)
    monkeypatch.setattr("llm.network_discovery.get_db", forbidden, raising=False)
    view = _public_server_view({**server, "endpoints": endpoints}, {})

    assert (view["private"], view["on_this_mac"]) == (False, True)


def test_a_send_in_an_ordinary_project_reads_no_server(world):
    """Item 5: with no policy in force the refusal returns after one project read."""
    from llm.locality import locality_refusal

    statements: list[str] = []
    world.set_trace_callback(statements.append)
    try:
        refusal = locality_refusal(
            {"litellm_model": LOCAL, "backend": "network", "provider": "omlx", "harness": "native"},
            thread_local_only=False, project_id=CLOUD_ON, db=world,
        )
    finally:
        world.set_trace_callback(None)

    assert refusal is None
    assert len(statements) == 1 and "project_inference_policies" in statements[0]


def test_a_catalog_build_reads_the_servers_once_and_endpoints_only_for_managed_ones(world, monkeypatch):
    import llm.network_discovery as nd
    import llm.network_endpoints as ne
    from llm.model_status import get_model_status_rows

    _server(world, "ext", LOOPBACK, kind="external")
    reads = {"servers": 0, "endpoints": []}
    real_servers, real_endpoints = nd.get_servers, ne.get_endpoints

    def servers(*args, **kwargs):
        reads["servers"] += 1
        return real_servers(*args, **kwargs)

    def endpoints(server_id):
        reads["endpoints"].append(server_id)
        return real_endpoints(server_id)

    monkeypatch.setattr(nd, "get_servers", servers)
    monkeypatch.setattr(ne, "get_endpoints", endpoints)

    get_model_status_rows()

    assert reads == {"servers": 1, "endpoints": ["loop"]}
