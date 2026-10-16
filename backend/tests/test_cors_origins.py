"""Credentialed CORS is disabled because every supported browser client is same-origin."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from infra import state

    monkeypatch.setattr(state, "_thread_driver_failure", None)
    return TestClient(app)


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "http://localhost:5173",  # Vite proxy development server.
        "http://127.0.0.1:5178",  # Playwright Vite proxy server.
        "http://localhost:5273",  # Tauri development server.
        "http://tauri.localhost",  # Tauri 2 static startup shell.
        "tauri://localhost",  # Tauri 2 static startup-shell origin.
    ],
)
def test_origin_bearing_requests_are_not_granted_cors(client: TestClient, origin: str):
    get_response = client.get("/api/health", headers={"Origin": origin})
    assert get_response.status_code == 200
    assert "access-control-allow-origin" not in get_response.headers

    preflight_response = client.options(
        "/api/health",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert preflight_response.status_code == 400
    assert "access-control-allow-origin" not in preflight_response.headers


def test_same_origin_request_without_origin_header_still_succeeds(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
