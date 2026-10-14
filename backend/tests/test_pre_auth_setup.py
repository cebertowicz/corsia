"""Pre-auth setup is strictly a direct-loopback first-run surface."""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import setup as setup_api  # noqa: E402
from infra import accounts  # noqa: E402
from infra.default_port import DEFAULT_BACKEND_PORT  # noqa: E402


LOOPBACK_BASE = "http:" + f"//127.0.0.1:{DEFAULT_BACKEND_PORT}"
LOOPBACK_HEADERS = {"Host": f"127.0.0.1:{DEFAULT_BACKEND_PORT}"}
JSON_HEADERS = {**LOOPBACK_HEADERS, "Content-Type": "application/json"}


@pytest.fixture
def app_client(isolated_db):
    app = FastAPI()
    app.include_router(setup_api.router)
    with TestClient(
        app,
        base_url=LOOPBACK_BASE,
        client=("127.0.0.1", 50100),
    ) as client:
        yield client


@pytest.fixture
def no_users(isolated_db):
    database = accounts._db(None)
    database.execute("DELETE FROM users")
    database.commit()


@pytest.fixture
def client(app_client, no_users):
    return app_client


def _post(client, path: str, body: dict | None = None, *, headers: dict | None = None):
    return client.post(path, json=body or {}, headers=headers or JSON_HEADERS)


@pytest.mark.parametrize("path", ["/api/setup/status", "/api/setup/local-download", "/api/setup/first-account"])
def test_refuses_wrong_host_without_detail(client, path):
    response = (
        client.get(path, headers={"Host": f"example.com:{DEFAULT_BACKEND_PORT}"})
        if path.endswith("status")
        else _post(client, path, {"username": "first", "password": "correct horse battery staple"}, headers={
            "Host": f"example.com:{DEFAULT_BACKEND_PORT}", "Content-Type": "application/json",
        })
    )
    assert response.status_code == 403
    assert response.content == b""


@pytest.mark.parametrize("forwarded", ["X-Forwarded-For", "Tailscale-User-Login"])
@pytest.mark.parametrize("path", ["/api/setup/status", "/api/setup/local-download", "/api/setup/first-account"])
def test_refuses_forwarded_requests_without_detail(client, path, forwarded):
    headers = {**JSON_HEADERS, forwarded: "forwarded"}
    response = (
        client.get(path, headers=headers)
        if path.endswith("status")
        else _post(client, path, {"username": "first", "password": "correct horse battery staple"}, headers=headers)
    )
    assert response.status_code == 403
    assert response.content == b""


@pytest.mark.parametrize("path", ["/api/setup/local-download", "/api/setup/first-account"])
def test_post_refuses_foreign_origin_and_form_data_without_detail(client, path):
    foreign = _post(client, path, {"username": "first", "password": "correct horse battery staple"}, headers={
        **JSON_HEADERS, "Origin": "https:" + "//example.com",
    })
    form = client.post(path, data={"username": "first", "password": "correct horse battery staple"}, headers=LOOPBACK_HEADERS)
    assert foreign.status_code == form.status_code == 403
    assert foreign.content == form.content == b""


def test_status_exposes_only_screen_facts(client, monkeypatch):
    facts = {
        "macos_apple_silicon": True,
        "ram_tier_gib": 64,
        "free_disk_bytes": 123,
        "app_location": "application_bundle",
        "local_eligible": True,
    }
    monkeypatch.setattr(setup_api, "_setup_status", lambda: facts)
    response = client.get("/api/setup/status", headers=LOOPBACK_HEADERS)
    assert response.status_code == 200
    assert response.json() == facts


@pytest.mark.parametrize("path", ["/api/setup/status", "/api/setup/local-download", "/api/setup/first-account"])
def test_post_setup_returns_complete_code(client, path):
    accounts.create_user("existing", "correct horse battery staple")
    response = (
        client.get(path, headers=LOOPBACK_HEADERS)
        if path.endswith("status")
        else _post(client, path, {"username": "first", "password": "correct horse battery staple"})
    )
    assert response.status_code == 409
    assert response.json() == {"code": "setup_complete"}


def test_local_download_uses_existing_single_flight(client, monkeypatch):
    from llm import local_setup

    calls = 0

    async def begin_local_setup():
        nonlocal calls
        calls += 1
        return {"ok": True, "in_progress": True, "stage": "Installing"}

    monkeypatch.setattr(local_setup, "begin_local_setup", begin_local_setup)
    first = _post(client, "/api/setup/local-download")
    second = _post(client, "/api/setup/local-download")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"ok": True, "in_progress": True, "stage": "Installing"}
    assert calls == 2


def test_first_account_issues_normal_browser_session(client):
    response = _post(client, "/api/setup/first-account", {
        "username": "First",
        "password": "correct horse battery staple",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["user"] == {"id": "first", "name": "first"}
    assert body["expires_at"]
    assert accounts.resolve_token(body["token"]) == {
        "id": "first", "name": "first", "token_kind": "session",
    }
    sessions = [row for row in accounts.list_tokens("first") if row["kind"] == "session"]
    assert len(sessions) == 1
    assert sessions[0]["label"] == "browser"


def test_concurrent_first_account_requests_have_one_winner(client):
    body = {"username": "first", "password": "correct horse battery staple"}

    def create():
        return _post(client, "/api/setup/first-account", body)

    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(workers.map(lambda _: create(), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert [response.json() for response in responses if response.status_code == 409] == [
        {"code": "setup_complete"},
    ]
    assert len(accounts.list_users()) == 1


def test_routes_are_registered():
    from main import app

    paths = set()
    for route in app.routes:
        included = getattr(route, "original_router", None)
        for entry in getattr(included, "routes", []) if included else [route]:
            path = getattr(entry, "path", None)
            if path:
                paths.add(path)
    assert {
        "/api/setup/status", "/api/setup/local-download", "/api/setup/first-account",
    } <= paths
