"""Unsupported hosts cannot start local-model setup or downloads."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import provider_local_models, provider_sources, setup
from infra.auth import require_user_header_only
from infra.default_port import DEFAULT_BACKEND_PORT
from llm import local_setup_flow, local_setup_gates, local_setup_prepare
from llm.local_model import LocalModel


ENDPOINTS = [
    ("post", "/api/setup/local-model", {}),
    ("post", "/api/setup/local-download", {}),
    ("post", "/api/providers/local/setup", {}),
    ("get", "/api/providers/local/recommendations", None),
    ("post", "/api/providers/local/downloads", {"repository": "org/model"}),
]


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    for router in (setup.router, provider_sources.router, provider_local_models.router):
        app.include_router(router)
    app.dependency_overrides[require_user_header_only] = lambda: {}
    async def no_users(*args, **kwargs):
        return False
    monkeypatch.setattr(setup, "run_in_db", no_users)
    with TestClient(app, base_url=f"http://127.0.0.1:{DEFAULT_BACKEND_PORT}", client=("127.0.0.1", 50100)) as client:
        yield client


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
@pytest.mark.parametrize("unsupported", [True, False], ids=["unsupported", "mac"])
def test_local_endpoint_host_gate(client, monkeypatch, method, path, body, unsupported):
    monkeypatch.setattr(local_setup_gates, "host_never_runs_local", lambda: unsupported)
    calls = []
    async def prepare():
        calls.append("prepare")
    async def settled():
        return {"ok": True}
    async def begin(**kwargs):
        calls.append("setup")
        return {"ok": True}
    model = LocalModel("org/model", 1_000_000, 32768, level=4.0, fp16=False, relative_speed=0.85)
    async def recommendations():
        calls.append("recommendations")
        return {"ok": True}, {"best": [model]}
    async def server():
        return {"id": "test"}
    async def directory(_server_id):
        return "/tmp"
    async def download(_model):
        calls.append("download")
        return {"ok": True}
    monkeypatch.setattr(local_setup_prepare, "begin_prepare", prepare)
    monkeypatch.setattr(local_setup_prepare, "settled_view", settled)
    monkeypatch.setattr(local_setup_flow, "begin_local_setup", begin)
    monkeypatch.setattr(provider_local_models, "_recommendations", recommendations)
    monkeypatch.setattr(provider_local_models, "_server", server)
    monkeypatch.setattr(provider_local_models, "download_dir", directory)
    monkeypatch.setattr(provider_local_models, "download_disk_space", lambda *args: {"state": "enough"})
    monkeypatch.setattr(provider_local_models.local_setup_operation, "read", lambda: None)
    monkeypatch.setattr(local_setup_flow, "begin_settings_download", download)
    response = getattr(client, method)(path, **({"json": body} if body is not None else {}))
    if unsupported:
        assert response.status_code == 409
        assert response.json() == {"detail": {
            "code": "never_local_host",
            "message": "Local models don't run on this computer. Connect a provider.",
        }}
        assert calls == []
    else:
        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True}
        assert calls
        if path.endswith("/downloads"):
            assert calls == ["recommendations", "download"]
