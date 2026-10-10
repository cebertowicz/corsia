from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


_BACKEND_DIR = Path(__file__).resolve().parents[1]


def _probe_target(target: str, *, mock_env: str | None) -> dict:
    env = os.environ.copy()
    if mock_env is None:
        env.pop("CORSIA_E2E_MOCK", None)
    else:
        env["CORSIA_E2E_MOCK"] = mock_env
    env.setdefault("CORSIA_ANALYTICS_DISABLE", "1")
    env.setdefault("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", "1")

    script = f"""
import importlib
import json
import sys

sys.path.insert(0, {str(_BACKEND_DIR)!r})
module = importlib.import_module({target!r})
app = module.app
importlib.import_module("core.checkpoint_runtime")

def flatten(routes, seen=None):
    seen = seen or set()
    paths = []
    for route in routes:
        marker = id(route)
        if marker in seen:
            continue
        seen.add(marker)
        path = getattr(route, "path", None)
        if path:
            paths.append(path)
        original = getattr(route, "original_router", None)
        if original is not None:
            paths.extend(flatten(getattr(original, "routes", []), seen))
        mounted = getattr(route, "app", None)
        if mounted is not None and mounted is not app:
            paths.extend(flatten(getattr(mounted, "routes", []), seen))
    return paths

print(json.dumps({{
    "paths": flatten(app.routes),
    "e2e_mock_imported": "e2e_mock" in sys.modules,
    "e2e_app_imported": "e2e_app" in sys.modules,
    "checkpoint_runtime_imported": "core.checkpoint_runtime" in sys.modules,
}}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=_BACKEND_DIR,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_production_target_cannot_enable_e2e_mocks_even_with_env():
    result = _probe_target("main", mock_env="1")
    assert "/api/test/mock-responses" not in result["paths"]
    assert result["checkpoint_runtime_imported"] is True
    assert result["e2e_mock_imported"] is False
    assert result["e2e_app_imported"] is False


def test_e2e_target_is_explicit_and_not_env_gated():
    result = _probe_target("e2e_app", mock_env=None)
    assert "/api/test/mock-responses" in result["paths"]
    assert result["e2e_mock_imported"] is True


def test_e2e_target_preserves_test_and_production_http_routes():
    from e2e_app import app

    client = TestClient(app)
    assert client.post(
        "/api/test/mock-responses", json={"replies": []}
    ).status_code == 200
    assert client.get("/api/health").json() == {"status": "ok"}
    production_openapi = client.get("/openapi.json")
    assert production_openapi.status_code == 200
    assert production_openapi.json()["info"]["title"] == "CORSIA UI"
    assert client.get("/route-owned-by-frontend-catch-all").status_code == 200


def test_e2e_target_preserves_production_websocket_route():
    from e2e_app import app, production_app

    root_mount = next(
        route for route in app.routes if getattr(route, "path", None) == ""
    )
    assert root_mount.app is production_app
    assert any(
        getattr(route, "path", None) == "/ws" for route in production_app.routes
    )


@pytest.mark.asyncio
async def test_e2e_lifespan_wraps_canonical_production_lifespan_once(monkeypatch):
    import e2e_app

    events: list[str] = []

    @contextmanager
    def mock_engine():
        events.append("mock enter")
        try:
            yield
        finally:
            events.append("mock exit")

    @asynccontextmanager
    async def production_lifespan(_app):
        events.append("production enter")
        try:
            yield
        finally:
            events.append("production exit")

    monkeypatch.setattr(e2e_app, "mock_engine", mock_engine)
    monkeypatch.setattr(
        e2e_app.production_app.router,
        "lifespan_context",
        production_lifespan,
    )
    monkeypatch.setattr(
        e2e_app,
        "seed_mock_catalog",
        lambda: events.append("seed"),
    )

    async with e2e_app._lifespan(e2e_app.app):
        events.append("serve")

    assert events == [
        "mock enter",
        "production enter",
        "seed",
        "serve",
        "production exit",
        "mock exit",
    ]
