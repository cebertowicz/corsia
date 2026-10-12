"""A server's key lives only in the key store: writes, reads, removal and who may write.

Every value here is synthetic.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from infra.key_store_servers import resolve_server, server_keys

FIELD = "api_key"
KEY = "disposable-server-key-0001"


@pytest.fixture
def api(isolated_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.network_servers import router
    from infra.auth import require_user
    from llm import network_discovery as nd
    from omlx_provider_fixtures import _patch_httpx, _resp, _Router

    _patch_httpx(monkeypatch, _Router({
        ("GET", "/health"): _resp(200, {"status": "ok"}),
        ("GET", "/v1/models"): _resp(200, {"data": []}),
    }))

    async def discovered(_server_id):
        return None

    monkeypatch.setattr(nd, "discover_and_notify", discovered)
    caller = {"id": "u-test", "name": "u-test", "token_kind": "session"}
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: caller
    with TestClient(app, raise_server_exceptions=False) as client:
        yield SimpleNamespace(client=client, caller=caller, path=isolated_db)


def _add(api, name="Studio", **extra):
    return api.client.post("/api/settings/network-servers", json={
        "name": name, "hostname": "127.0.0.1", "port": 5200, FIELD: KEY, **extra,
    } if "hostname" not in extra else {"name": name, "port": 5200, FIELD: KEY, **extra})


def _metadata(path) -> list:
    with closing(sqlite3.connect(str(path))) as db:
        return [r[0] for r in db.execute("SELECT metadata_json FROM network_servers")]


def test_an_added_key_is_stored_and_never_written_to_metadata(api):
    response = _add(api)

    assert response.status_code == 200
    server_id = response.json()["server_id"]
    assert resolve_server(server_id) == KEY
    assert KEY not in json.dumps(_metadata(api.path))
    assert KEY not in response.text


def _broken(*_args, **_kwargs):
    raise RuntimeError("metadata write failed")


@pytest.mark.parametrize("kind, failure", [("omlx", "commit"), ("external", "host"), ("external", "commit")])
def test_a_failed_add_registers_nothing_and_no_value_survives_a_sweep(api, monkeypatch, kind, failure):
    from unittest.mock import AsyncMock

    from api import network_server_helpers as helpers
    from key_store_fixtures import fail_the_commit_after_a_key_write, values_after_a_sweep
    from llm import network_discovery as nd

    monkeypatch.setattr(helpers, "_probe_external", AsyncMock(return_value=[{"id": "m"}]))
    extra = {"hostname": "", "base_url": "https://external.example.com/v1", "dialect": "openai"}
    with monkeypatch.context() as patched:
        if failure == "host":
            patched.setattr(nd, "set_server_metadata", _broken)
        else:
            fail_the_commit_after_a_key_write(patched)
        response = _add(api, **(extra if kind == "external" else {}))

    assert response.status_code == 500
    with closing(sqlite3.connect(str(api.path))) as db:
        assert db.execute("SELECT COUNT(*) FROM network_servers").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM key_store_entries").fetchone()[0] == 0
    assert values_after_a_sweep() == {}


def test_a_planted_metadata_key_is_not_read(api):
    server_id = _add(api, name="Other").json()["server_id"]
    with closing(sqlite3.connect(str(api.path))) as db:
        db.execute("UPDATE network_servers SET metadata_json = ? WHERE id = ?",
                   (json.dumps({FIELD: "planted-0002"}), server_id))
        db.commit()

    assert resolve_server(server_id) == KEY
    listed = api.client.get("/api/settings/network-servers").json()["servers"]
    assert "planted-0002" not in json.dumps(listed)


def test_has_key_and_mask_come_from_the_store(api):
    keyed = _add(api, name="Keyed").json()["server_id"]
    bare = api.client.post("/api/settings/network-servers", json={
        "name": "Bare", "hostname": "127.0.0.2", "port": 5200,
    }).json()["server_id"]

    servers = {s["id"]: s for s in api.client.get("/api/settings/network-servers").json()["servers"]}

    assert servers[keyed]["has_key"] is True and servers[keyed]["masked_key"].endswith(KEY[-4:])
    assert KEY not in json.dumps(servers)
    assert servers[bare]["has_key"] is False and servers[bare]["masked_key"] == ""
    assert set(server_keys([keyed, bare])) == {keyed}


def test_removing_a_server_drops_its_key(api):
    server_id = _add(api).json()["server_id"]

    assert api.client.delete(f"/api/settings/network-servers/{server_id}").status_code == 200

    assert resolve_server(server_id) is None
    with closing(sqlite3.connect(str(api.path))) as db:
        assert db.execute("SELECT 1 FROM key_store_entries WHERE name = ?", (f"server-{server_id}",)).fetchone() is None


@pytest.mark.parametrize("kind", ["api", "lifecycle"])
def test_only_a_session_may_send_a_key(api, kind):
    server_id = _add(api).json()["server_id"]
    api.caller["token_kind"] = kind

    added = _add(api, name="Second")
    edited = api.client.put(f"/api/settings/network-servers/{server_id}", json={
        "base_url": "https://example.test/v1", FIELD: "replacement-0003",
    })

    for response in (added, edited):
        assert response.status_code == 403
        assert response.json()["detail"] == "Enter the key in Providers while signed in."
    assert resolve_server(server_id) == KEY
    assert len(_metadata(api.path)) == 1


def test_a_non_session_token_may_still_add_a_server_without_a_key(api):
    api.caller["token_kind"] = "api"

    response = api.client.post("/api/settings/network-servers", json={
        "name": "Bare", "hostname": "127.0.0.3", "port": 5200,
    })

    assert response.status_code == 200
    assert resolve_server(response.json()["server_id"]) is None


def test_local_setup_stores_its_key_through_the_internal_writer(api):
    from api.network_server_helpers import _add_omlx_server

    result = asyncio.run(_add_omlx_server(
        SimpleNamespace(hostname="127.0.0.1", port=8000, name="This Mac", **{FIELD: KEY}),
    ))

    assert resolve_server(result["server_id"]) == KEY
    assert KEY not in json.dumps(_metadata(api.path))


def test_settings_keys_lists_each_live_server_with_presence_and_no_value(api):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.keys import router as keys_router
    from infra.auth import require_user

    keyed = _add(api, name="Keyed").json()["server_id"]
    bare = api.client.post("/api/settings/network-servers", json={
        "name": "Bare", "hostname": "127.0.0.2", "port": 5200,
    }).json()["server_id"]
    gone = _add(api, name="Gone", hostname="127.0.0.4").json()["server_id"]
    api.client.delete(f"/api/settings/network-servers/{gone}")
    app = FastAPI()
    app.include_router(keys_router)
    app.dependency_overrides[require_user] = lambda: api.caller

    response = TestClient(app).get("/api/keys")

    rows = {row["server_id"]: row for row in response.json()["server_keys"]}
    assert set(rows) == {keyed, bare}
    assert rows[keyed] == {"name": f"server-{keyed}", "server_id": keyed, "label": "Keyed", "is_set": True}
    assert rows[bare]["is_set"] is False and rows[bare]["label"] == "Bare"
    assert KEY not in response.text


def test_keyed_omlx_discovery_and_model_list_send_the_stored_key(isolated_db, monkeypatch, caplog):
    """Both pass no key and name only the server id; the stored key still authenticates every public call."""
    import httpx

    from api.network_server_helpers import list_daemon_models
    from infra.key_store_servers import set_server_key
    from llm import network_discovery as nd
    from llm.catalog_network_models import discover_network_models

    server_id = nd.upsert_server("omlx", "127.0.0.1:5200", "Studio", "127.0.0.1", 5200, "online")
    set_server_key(server_id, KEY)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/models/status":
            return httpx.Response(200, json={"models": []}, request=request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": []}, request=request)
        return httpx.Response(200, json={}, request=request)

    original = httpx.AsyncClient

    class Client(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    caplog.set_level("DEBUG")

    asyncio.run(discover_network_models(server_id))
    discovery_calls = len(requests)
    asyncio.run(list_daemon_models(server_id, nd.get_server(server_id)))

    public = [r for r in requests if r.url.path.startswith("/v1/") or r.url.path == "/admin/api/status"]
    assert discovery_calls and len(requests) > discovery_calls
    assert {r.url.path for r in public} >= {"/v1/models/status"}
    assert all(r.headers.get("authorization") == f"Bearer {KEY}" for r in public)
    assert KEY not in caplog.text
