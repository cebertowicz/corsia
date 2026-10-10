from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth_fixtures import OWNER_TOKEN


def test_system_reports_the_recorded_install_choice(isolated_db, monkeypatch, tmp_path):
    import infra.config as config
    from api import system

    bootstrap = tmp_path / "config.json"
    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", bootstrap)
    monkeypatch.setitem(system._system_cache, "data", {})
    monkeypatch.setitem(system._system_cache, "ts", 1e18)
    app = FastAPI()
    app.include_router(system.router)
    headers = {"Authorization": f"Bearer {OWNER_TOKEN}"}

    def read() -> object:
        with TestClient(app) as client:
            return client.get("/api/system", headers=headers).json()["network_setup"]

    assert read() is None
    bootstrap.write_text('{"setup": "server", "bind_host": "0.0.0.0"}')
    assert read() == "server"
    bootstrap.write_text('{"setup": "local", "bind_host": "127.0.0.1"}')
    assert read() == "local"
