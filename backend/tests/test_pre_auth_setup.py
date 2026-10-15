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
from infra import accounts, first_account  # noqa: E402
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


def test_refuses_loopback_host_for_a_different_port_without_detail(client):
    response = client.get("/api/setup/status", headers={"Host": f"127.0.0.1:{DEFAULT_BACKEND_PORT + 1}"})
    assert response.status_code == 403
    assert response.content == b""


def test_accepts_loopback_host_for_its_own_nondefault_port(no_users):
    port = 43123
    app = FastAPI()
    app.include_router(setup_api.router)
    with TestClient(app, base_url=f"http://127.0.0.1:{port}", client=("127.0.0.1", 50101)) as nondefault_client:
        response = nondefault_client.get("/api/setup/status", headers={"Host": f"127.0.0.1:{port}"})
    assert response.status_code == 200


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
        "required_username": None,
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
    monkeypatch.setattr(first_account, "seed_workspace_profile", lambda name, email: seeded.append((name, email)))
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


def _own_data(owner: str) -> None:
    from infra.time import utc_now_iso

    database = accounts._db(None)
    now = utc_now_iso()
    database.execute(
        "INSERT INTO threads (id, user_id, title, created_at, updated_at) VALUES (?, ?, 'Old', ?, ?)",
        (f"t-{owner}", owner, now, now),
    )
    database.commit()


def test_first_account_seeding_failure_is_an_error_and_leaves_no_account(client, monkeypatch, caplog):
    def fail_profile_seed(_name, _email):
        raise RuntimeError("profile store unavailable")

    monkeypatch.setattr(first_account, "seed_workspace_profile", fail_profile_seed)
    response = _post(client, "/api/setup/first-account", {
        "name": "First Person", "username": "first", "password": "correct horse battery staple",
        "email": "first@example.test",
    })

    assert response.status_code == 500
    assert response.json()["code"] == "setup_failed"
    assert accounts.list_users() == []
    assert accounts.list_tokens("first") == []
    assert "First-account creation failed: RuntimeError" in caplog.text
    assert "First Person" not in caplog.text
    assert "first@example.test" not in caplog.text


def test_an_unwritable_profile_fails_account_creation_instead_of_passing_silently(client, monkeypatch, tmp_path):
    from infra import config

    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(config, "WORKSPACE", blocker)
    response = _post(client, "/api/setup/first-account", {
        "name": "First Person", "username": "first", "password": "a-long-enough-passphrase",
    })

    assert response.status_code == 500
    assert response.json()["code"] == "setup_failed"
    assert accounts.list_users() == []


def test_first_account_refuses_a_name_outside_the_data_owners(client, monkeypatch):
    monkeypatch.setattr(first_account, "seed_workspace_profile", lambda *_args: None)
    _own_data("legacy-owner")
    response = _post(client, "/api/setup/first-account", {
        "name": "First Person", "username": "someone-else", "password": "correct horse battery staple",
    })

    assert response.status_code == 422
    assert "owns none of the existing data" in response.json()["detail"]
    assert accounts.list_users() == []
    assert accounts.list_tokens("someone-else") == []


def test_first_account_accepts_the_required_data_owner_name(client, monkeypatch):
    monkeypatch.setattr(first_account, "seed_workspace_profile", lambda *_args: None)
    _own_data("legacy-owner")
    response = _post(client, "/api/setup/first-account", {
        "name": "First Person", "username": "legacy-owner", "password": "correct horse battery staple",
    })

    assert response.status_code == 200
    assert response.json()["user"] == {"id": "legacy-owner", "name": "legacy-owner"}
    assert [user["id"] for user in accounts.list_users()] == ["legacy-owner"]


def test_status_names_the_data_owner_only_when_there_is_exactly_one(client, monkeypatch):
    monkeypatch.setattr(setup_api, "_setup_status", setup_api._setup_status)
    assert client.get("/api/setup/status", headers=LOOPBACK_HEADERS).json()["required_username"] is None
    _own_data("legacy-owner")
    assert client.get("/api/setup/status", headers=LOOPBACK_HEADERS).json()["required_username"] == "legacy-owner"
    _own_data("other-owner")
    assert client.get("/api/setup/status", headers=LOOPBACK_HEADERS).json()["required_username"] is None


def test_first_account_returns_safe_account_error_detail(client):
    response = _post(client, "/api/setup/first-account", {
        "name": "First Person", "username": "not valid", "password": "[REDACTED]",
    })

    assert response.status_code == 422
    assert response.json() == {"detail": "username must be 1-32 characters of a-z, 0-9, '-' or '_'"}


def test_first_account_rejects_the_client_only_folder_field(client):
    response = _post(client, "/api/setup/first-account", {
        "name": "First Person", "username": "first", "password": "correct horse battery staple", "folder": "~/corsia",
    })
    assert response.status_code == 422


def test_concurrent_first_account_requests_have_one_winner(client, monkeypatch):
    monkeypatch.setattr(first_account, "seed_workspace_profile", lambda *_args: None)
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
        (Path("/private/var/folders/x/AppTranslocation/id/d/CORSIA.app/Contents/MacOS/corsia"), "disk_image"),
        (Path("/private/Applications/cache/corsia"), "other"),
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


def test_profile_seeding_is_one_helper_shared_by_the_cli_and_the_setup_route(monkeypatch, tmp_path):
    import cli_accounts
    from infra import config, user_profile

    writes = []
    additions = []
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(user_profile, "mac_timezone", lambda: "America/Los_Angeles")
    monkeypatch.setattr(
        user_profile, "write_user_profile",
        lambda workspace, name, timezone, email: writes.append((workspace, name, timezone, email)) or False,
    )
    monkeypatch.setattr(user_profile, "add_profile_email", lambda workspace, email: additions.append((workspace, email)))
    profile_path = tmp_path / user_profile.PROFILE_RELATIVE_PATH
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text("# User profile\n", encoding="utf-8")
    first_account.seed_workspace_profile("First\nPerson\x00", "first@example.test")
    assert writes == [(tmp_path, "FirstPerson", "America/Los_Angeles", "first@example.test")]
    assert additions == [(tmp_path, "first@example.test")]
    assert not hasattr(cli_accounts, "_seed_user_profile")
    assert not hasattr(setup_api, "_seed_workspace_profile")


def test_cli_and_setup_route_write_the_same_profile(client, monkeypatch, tmp_path):
    import argparse
    import io

    import cli_accounts
    from infra import config, user_profile

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(user_profile, "mac_timezone", lambda: "America/Los_Angeles")
    profile = tmp_path / user_profile.PROFILE_RELATIVE_PATH
    response = _post(client, "/api/setup/first-account", {
        "name": "Pat", "username": "pat", "password": "correct horse battery staple",
        "email": "pat@example.test",
    })
    assert response.status_code == 200
    from_route = profile.read_text()

    database = accounts._db(None)
    database.execute("DELETE FROM users")
    database.commit()
    profile.unlink()
    answers = ["Pat", "pat@example.test"]
    stdin = io.StringIO()
    stdin.isatty = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr("builtins.input", lambda prompt="": answers.pop(0))
    monkeypatch.setattr(cli_accounts.getpass, "getpass", lambda prompt="": "correct horse battery staple")
    monkeypatch.setattr(cli_accounts, "_ensure_schema", lambda: None)
    assert cli_accounts.cmd_user_add(argparse.Namespace(name="pat", password_stdin=False)) == 0
    assert profile.read_text() == from_route


def test_status_has_a_stable_below_minimum_ram_variant(monkeypatch, tmp_path):
    from llm import local_setup_gates

    monkeypatch.setattr(local_setup_gates, "host_ram_bytes", lambda: 1)
    monkeypatch.setattr(setup_api.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(setup_api.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(setup_api.platform, "mac_ver", lambda: ("15.0.0", ("", "", ""), ""))
    monkeypatch.setattr(setup_api.config, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(setup_api.config, "WORKSPACE", tmp_path / "workspace")
    status = setup_api._setup_status()
    assert status["ram_tier"] == "below_minimum"
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
