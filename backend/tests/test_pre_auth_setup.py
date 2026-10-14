"""Pre-auth setup is strictly a direct-loopback first-run surface."""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
        else _post(client, path, {"name": "First", "username": "first", "password": "correct horse battery staple"}, headers={
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
        else _post(client, path, {"name": "First", "username": "first", "password": "correct horse battery staple"}, headers=headers)
    )
    assert response.status_code == 403
    assert response.content == b""


@pytest.mark.parametrize("path", ["/api/setup/local-download", "/api/setup/first-account"])
def test_post_refuses_foreign_origin_and_form_data_without_detail(client, path):
    foreign = _post(client, path, {"name": "First", "username": "first", "password": "correct horse battery staple"}, headers={
        **JSON_HEADERS, "Origin": "https:" + "//example.com",
    })
    form = client.post(path, data={"username": "first", "password": "correct horse battery staple"}, headers=LOOPBACK_HEADERS)
    assert foreign.status_code == form.status_code == 403
    assert foreign.content == form.content == b""


def test_status_exposes_only_screen_facts(client, monkeypatch):
    facts = {
        "apple_silicon": True,
        "macos_supported": True,
        "ram_tier": "eligible",
        "free_disk_gb": 123.0,
        "app_location": "applications",
        "local_eligible": True,
        "work_folder": "~/corsia",
        "model_name": "Example model",
        "model_size_gb": 30.0,
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
        else _post(client, path, {"name": "First", "username": "first", "password": "correct horse battery staple"})
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


def test_first_account_issues_normal_browser_session(client, monkeypatch):
    seeded = []
    monkeypatch.setattr(setup_api, "_seed_workspace_profile", lambda name, email: seeded.append((name, email)))
    response = _post(client, "/api/setup/first-account", {
        "name": "First Person",
        "username": "First",
        "password": "correct horse battery staple",
        "email": "first@example.test",
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
    assert seeded == [("First Person", "first@example.test")]


def test_first_account_rejects_the_client_only_folder_field(client):
    response = _post(client, "/api/setup/first-account", {
        "name": "First Person", "username": "first", "password": "correct horse battery staple", "folder": "~/corsia",
    })
    assert response.status_code == 422


def test_concurrent_first_account_requests_have_one_winner(client, monkeypatch):
    monkeypatch.setattr(setup_api, "_seed_workspace_profile", lambda *_args: None)
    body = {"name": "First", "username": "first", "password": "correct horse battery staple"}

    def create():
        return _post(client, "/api/setup/first-account", body)

    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(workers.map(lambda _: create(), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert [response.json() for response in responses if response.status_code == 409] == [
        {"code": "setup_complete"},
    ]
    assert len(accounts.list_users()) == 1


@pytest.mark.parametrize(
    ("system", "version", "expected"),
    [("Darwin", "15.0.0", True), ("Darwin", "14.6.1", False), ("Linux", "15.0.0", False), ("Darwin", "unknown", False)],
)
def test_macos_support_uses_the_approved_major_version_floor(monkeypatch, system, version, expected):
    monkeypatch.setattr(setup_api.platform, "system", lambda: system)
    monkeypatch.setattr(setup_api.platform, "mac_ver", lambda: (version, ("", "", ""), ""))
    assert setup_api._macos_supported() is expected


@pytest.mark.parametrize(
    ("executable", "expected"),
    [
        (Path("/Applications/CORSIA.app/Contents/MacOS/corsia"), "applications"),
        (Path("/Volumes/CORSIA/CORSIA.app/Contents/MacOS/corsia"), "disk_image"),
        (Path("/opt/corsia/bin/corsia"), "other"),
    ],
)
def test_app_location_is_classified_without_returning_a_path(executable, expected):
    assert setup_api._app_location_class(executable) == expected


def test_work_folder_display_hides_the_home_name(monkeypatch, tmp_path):
    home = tmp_path / "person"
    monkeypatch.setattr(setup_api.Path, "home", lambda: home)
    assert setup_api._work_folder_display(home / "corsia") == "~/corsia"
    assert setup_api._work_folder_display(tmp_path / "shared" / "workspace") == "…/workspace"


def test_profile_seeding_matches_the_cli_create_or_add_email_behavior(monkeypatch, tmp_path):
    from infra import user_profile

    writes = []
    additions = []
    monkeypatch.setattr(setup_api.config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(user_profile, "mac_timezone", lambda: "America/Los_Angeles")
    monkeypatch.setattr(
        user_profile, "write_user_profile",
        lambda workspace, name, timezone, email: writes.append((workspace, name, timezone, email)) or False,
    )
    monkeypatch.setattr(user_profile, "add_profile_email", lambda workspace, email: additions.append((workspace, email)))
    setup_api._seed_workspace_profile("First Person", "first@example.test")
    assert writes == [(tmp_path, "First Person", "America/Los_Angeles", "first@example.test")]
    assert additions == [(tmp_path, "first@example.test")]


def test_status_has_a_stable_low_ram_variant(monkeypatch, tmp_path):
    from llm import local_setup_gates

    monkeypatch.setattr(local_setup_gates, "host_ram_bytes", lambda: 1)
    monkeypatch.setattr(setup_api.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(setup_api.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(setup_api.platform, "mac_ver", lambda: ("15.0.0", ("", "", ""), ""))
    monkeypatch.setattr(setup_api.config, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(setup_api.config, "WORKSPACE", tmp_path / "workspace")
    status = setup_api._setup_status()
    assert status["ram_tier"] == "low"
    assert status["local_eligible"] is False
    assert status["free_disk_gb"] is not None
    assert status["work_folder"] == "…/workspace"


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
