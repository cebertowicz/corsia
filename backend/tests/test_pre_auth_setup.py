"""Pre-auth setup is strictly a direct-loopback first-run surface."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import setup as setup_api  # noqa: E402
from api import system as system_api  # noqa: E402
from infra import accounts, token_admin  # noqa: E402
from infra.default_port import DEFAULT_BACKEND_PORT  # noqa: E402
from omlx_recommender_fixtures import best_rows, fresh_local_setup  # noqa: E402,F401


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
def system_client(isolated_db):
    app = FastAPI()
    app.include_router(system_api.router)
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
def workspace(tmp_path, monkeypatch):
    """A throwaway workspace, so no first-account test writes a real profile."""
    from infra import config

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    return tmp_path


@pytest.fixture
def client(app_client, no_users, workspace):
    return app_client


def _post(client, path: str, body: dict | None = None, *, headers: dict | None = None):
    return client.post(path, json=body or {}, headers=headers or JSON_HEADERS)


# Every pre-auth route, as the app calls it.
ROUTES = [
    ("GET", "/api/setup/status"), ("POST", "/api/setup/local-download"), ("POST", "/api/setup/local-download/stop"), ("POST", "/api/setup/first-account"),
    ("GET", "/api/setup/local-model"), ("POST", "/api/setup/local-model"), ("POST", "/api/setup/local-model/stop"),
]
POSTS = [path for method, path in ROUTES if method == "POST"]
ACCOUNT_BODY = {"name": "First", "username": "first", "password": "correct horse battery staple"}


def _call(client, method: str, path: str, headers: dict):
    if method == "GET":
        return client.get(path, headers=headers)
    return _post(client, path, ACCOUNT_BODY, headers=headers)


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_refuses_wrong_host_without_detail(client, method, path):
    response = _call(client, method, path, {
        "Host": f"example.com:{DEFAULT_BACKEND_PORT}", "Content-Type": "application/json",
    })
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
@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_refuses_forwarded_requests_without_detail(client, method, path, forwarded):
    response = _call(client, method, path, {**JSON_HEADERS, forwarded: "forwarded"})
    assert response.status_code == 403
    assert response.content == b""


@pytest.mark.parametrize("path", POSTS)
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
        "local_eligible": True,
        "work_folder": "~/corsia",
        "required_username": None,
        "existing_omlx": False,
        "ready_ai": None,
    }
    monkeypatch.setattr(setup_api, "_setup_status", lambda: facts)
    response = client.get("/api/setup/status", headers=LOOPBACK_HEADERS)
    assert response.status_code == 200
    assert response.json() == facts


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_post_setup_returns_complete_code(client, method, path):
    accounts.create_user("existing", "correct horse battery staple")
    response = _call(client, method, path, JSON_HEADERS)
    assert response.status_code == 409
    assert response.json() == {"code": "setup_complete"}


def test_polling_setup_status_and_local_download_spawn_no_processes(client, monkeypatch):
    from llm import local_setup

    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())

    def forbidden(*_args, **_kwargs):
        pytest.fail("polling must not inspect processes")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)

    status = client.get("/api/setup/status", headers=LOOPBACK_HEADERS)
    local_download = client.get("/api/setup/local-download", headers=LOOPBACK_HEADERS)

    assert status.status_code == local_download.status_code == 200
    assert "existing_omlx" not in status.json()
    assert local_download.json()["state"] == "stopped"


@pytest.mark.usefixtures("mac_local_model_host")
def test_local_download_uses_the_single_managed_flight(client, monkeypatch):
    from llm import local_setup_flow as flow

    calls = 0

    async def begin_local_setup(*, onboarding):
        assert onboarding is True
        nonlocal calls
        calls += 1
        return {"ok": True, "in_progress": True, "stage": "Installing"}

    monkeypatch.setattr(flow, "begin_local_setup", begin_local_setup)
    first = _post(client, "/api/setup/local-download")
    second = _post(client, "/api/setup/local-download")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"ok": True, "in_progress": True, "stage": "Installing"}
    assert calls == 2


def test_operation_active_accepts_raw_loopback_shape_without_origin(system_client, monkeypatch):
    async def no_operation(_callback):
        return None

    monkeypatch.setattr(system_api, "run_in_db", no_operation)

    response = system_client.get(
        "/api/system/operation-active",
        headers={"Host": f"127.0.0.1:{DEFAULT_BACKEND_PORT}", "Connection": "close"},
    )

    assert response.status_code == 200
    assert response.json() == {"active": False}


@pytest.mark.parametrize(
    ("client_address", "headers"),
    [
        (("198.51.100.1", 50100), LOOPBACK_HEADERS),
        (("127.0.0.1", 50100), {**LOOPBACK_HEADERS, "Origin": "https://example.com"}),
    ],
)
def test_operation_active_refuses_untrusted_requests(client_address, headers, isolated_db):
    app = FastAPI()
    app.include_router(system_api.router)
    with TestClient(app, base_url=LOOPBACK_BASE, client=client_address) as client:
        response = client.get("/api/system/operation-active", headers=headers)

    assert response.status_code == 403


def test_first_account_route_uses_the_app_path(client, monkeypatch):
    calls = []
    original = accounts.create_first_account

    def capture(*args, **kwargs):
        calls.append(kwargs["first_run_path"])
        return original(*args, **kwargs)

    monkeypatch.setattr(accounts, "create_first_account", capture)
    response = _post(client, "/api/setup/first-account", {
        "name": "First Person", "username": "first", "password": "[REDACTED]",
    })
    assert response.status_code == 200
    assert calls == ["app"]


def test_first_account_issues_normal_browser_session(client, workspace):
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
    sessions = [row for row in token_admin.list_tokens("first") if row["kind"] == "session"]
    assert len(sessions) == 1
    assert sessions[0]["label"] == "browser"
    profile = (workspace / "context" / "50-USER-PROFILE.md").read_text(encoding="utf-8")
    assert "- **Preferred name:** First\n- **Full name:** First Person\n" in profile
    assert "- **Email:** first@example.test\n" in profile


def _own_data(owner: str) -> None:
    from infra.time import utc_now_iso

    database = accounts._db(None)
    now = utc_now_iso()
    database.execute(
        "INSERT INTO threads (id, user_id, title, created_at, updated_at) VALUES (?, ?, 'Old', ?, ?)",
        (f"t-{owner}", owner, now, now),
    )
    database.commit()


def test_first_account_refuses_a_name_outside_the_data_owners(client, monkeypatch):
    _own_data("legacy-owner")
    response = _post(client, "/api/setup/first-account", {
        "name": "First Person", "username": "someone-else", "password": "correct horse battery staple",
    })

    assert response.status_code == 422
    assert "owns none of the existing data" in response.json()["detail"]
    assert accounts.list_users() == []
    assert token_admin.list_tokens("someone-else") == []


def test_first_account_accepts_the_required_data_owner_name(client, monkeypatch):
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
    assert response.json() == {"detail": "Use up to 32 lowercase letters, numbers, dashes or underscores for the username."}


def test_first_account_rejects_the_client_only_folder_field(client):
    response = _post(client, "/api/setup/first-account", {
        "name": "First Person", "username": "first", "password": "correct horse battery staple", "folder": "~/corsia",
    })
    assert response.status_code == 422


def test_concurrent_first_account_requests_have_one_winner(client, monkeypatch):
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
    [("Darwin", "15.0.0", True), ("Darwin", "14.6.1", False), ("Linux", "15.0.0", None), ("Darwin", "unknown", False)],
)
def test_macos_support_uses_the_approved_major_version_floor(monkeypatch, system, version, expected):
    monkeypatch.setattr(setup_api.platform, "system", lambda: system)
    monkeypatch.setattr(setup_api.platform, "mac_ver", lambda: (version, ("", "", ""), ""))
    assert setup_api._macos_supported() is expected



def test_status_off_macos_is_not_blocked_by_the_macos_floor_and_not_local_eligible(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_api.platform, "system", lambda: "Linux")
    monkeypatch.setattr(setup_api.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(setup_api.config, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(setup_api.config, "WORKSPACE", tmp_path / "workspace")
    status = setup_api._setup_status()
    assert status["macos_supported"] is None
    assert status["apple_silicon"] is False
    assert status["local_eligible"] is False


def test_setup_status_takes_not_macos_from_the_one_host_function(monkeypatch, tmp_path):
    monkeypatch.setattr("llm.local_setup_gates.host_never_runs_local", lambda: True)
    monkeypatch.setattr(setup_api.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(setup_api.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(setup_api.config, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(setup_api.config, "WORKSPACE", tmp_path / "workspace")
    status = setup_api._setup_status()
    assert status["macos_supported"] is None and status["apple_silicon"] is False


def test_work_folder_display_hides_the_home_name(monkeypatch, tmp_path):
    home = tmp_path / "person"
    monkeypatch.setattr(setup_api.Path, "home", lambda: home)
    assert setup_api._work_folder_display(home / "corsia") == "~/corsia"
    assert setup_api._work_folder_display(tmp_path / "shared" / "workspace") == "…/workspace"


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


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ({}, None),
        ({"claude-code": "needs_auth", "codex": "not_installed"}, None),
        ({"claude-code": "needs_auth", "codex": "ready"}, "Codex"),
        ({"claude-code": "ready", "codex": "ready"}, "Claude Code"),
    ],
)
def test_status_names_a_ready_cli_source_from_its_accounts(monkeypatch, states, expected):
    from connections import source_state

    monkeypatch.setattr(source_state, "harness_state",
                        lambda provider: {"state": states.get(provider, "not_installed"), "checked_at": None})
    assert setup_api._ready_ai() == expected


@pytest.mark.parametrize(
    ("views", "expected"),
    [
        ({}, False),
        ({"claude-code": ("unverified", None)}, True),
        ({"claude-code": ("unverified", "t1"), "codex": ("ready", "t2")}, False),
    ],
)
def test_status_says_the_check_is_pending_while_a_signed_in_account_is_unchecked(monkeypatch, views, expected):
    from connections import source_state

    def view(provider):
        state, checked_at = views.get(provider, ("needs_auth", None))
        return {"state": state, "checked_at": checked_at}

    monkeypatch.setattr(source_state, "harness_state", view)
    assert setup_api._ai_check_pending() is expected


def test_routes_are_registered():
    from main import app

    paths = set()
    for route in app.routes:
        included = getattr(route, "original_router", None)
        for entry in getattr(included, "routes", []) if included else [route]:
            path = getattr(entry, "path", None)
            if path:
                paths.add(path)
    assert {path for _method, path in ROUTES} <= paths


@pytest.fixture
def welcome(no_users, monkeypatch, fresh_local_setup, mac_local_model_host):
    """The setup routes on a non-default bound port, as the app reaches them before any account."""
    from llm import local_setup

    port = 43124
    headers = {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"}
    app = FastAPI()
    app.include_router(setup_api.router)
    with TestClient(app, base_url=f"http://127.0.0.1:{port}", client=("127.0.0.1", 50102)) as client:
        def call(method: str, path: str, *, host_port: int = port):
            sent = {**headers, "Host": f"127.0.0.1:{host_port}"}
            return client.get(path, headers=sent) if method == "GET" else client.post(path, json={}, headers=sent)

        def until(done, what: str):
            # The app's event loop runs prepare and the flight; give it real time.
            deadline = time.monotonic() + 5
            while not done():
                assert time.monotonic() < deadline, f"timed out waiting for {what}"
                time.sleep(0.01)

        yield SimpleNamespace(call=call, until=until, flight=lambda: local_setup._flight)


def test_welcome_picks_then_get_started_downloads_that_model_through_the_setup_routes(welcome, monkeypatch):
    from omlx_recommender_fixtures import mac_with

    mac = mac_with(monkeypatch, 48)

    assert welcome.call("GET", "/api/setup/local-model").json()["state"] == "not_checked"
    # The POST answers once oMLX has picked: Welcome has no event to wait on before the account.
    picked = welcome.call("POST", "/api/setup/local-model").json()
    assert welcome.call("GET", "/api/setup/local-model").json() == picked
    row = next(row for row in best_rows(48) if row["repo_id"] == "Jundot/Qwen3.8-27B-oQ4e-mtp")
    assert picked == {"state": "picked", "message": None, "model": {
        "name": "Qwen3.8 27B", "repository": row["repo_id"], "model": "Qwen3.8-27B-oQ4e-mtp",
        "download_size_bytes": row["download_bytes"], "download_size_gb": round(row["download_bytes"] / 1000**3, 1),
        "context_window": 262_144, "quantization_bits": row["level"], "relative_speed": row["relative_speed"],
        "resident_bytes": row["resident_bytes"],
        "smarts_level": row["smarts_level"], "tokens_per_second": row["tokens_per_second"], "smarts": row["smarts"],
    }}
    # Welcome only picked; nothing downloaded and nothing was written to oMLX.
    assert mac.downloads == [] and mac.omlx.puts() == []
    # The bound port is part of trust: the same request for another port is refused.
    assert welcome.call("GET", "/api/setup/local-model", host_port=43125).status_code == 403

    started = welcome.call("POST", "/api/setup/local-download")
    assert started.status_code == 200 and started.json()["ok"] is True
    welcome.until(lambda: welcome.flight().task is not None and welcome.flight().task.done(), "the flight")

    assert welcome.flight().result["ok"] is True
    # Get started downloaded Welcome's pick, without asking the recommender again.
    assert [model.repository for model in mac.downloads] == ["Jundot/Qwen3.8-27B-oQ4e-mtp"]
    assert mac.omlx.asks() == 1


def test_turning_the_switch_off_on_welcome_stops_the_pick_through_the_setup_routes(welcome, monkeypatch):
    from omlx_recommender_fixtures import mac_with

    mac = mac_with(monkeypatch, 64)
    assert welcome.call("POST", "/api/setup/local-model").json()["state"] == "picked"

    stopped = welcome.call("POST", "/api/setup/local-model/stop").json()

    assert stopped == {"state": "not_checked", "model": None, "message": None}
    assert mac.stopped == ["srv"] and mac.downloads == []


def test_a_mac_under_24_gb_gets_no_pick_and_starts_nothing_through_the_setup_routes(welcome, monkeypatch):
    from llm.local_setup_prepare import NO_LOCAL_MODEL
    from omlx_recommender_fixtures import mac_with

    mac = mac_with(monkeypatch, 16)

    view = welcome.call("POST", "/api/setup/local-model").json()

    assert view == {"state": "low_memory", "model": None, "message": NO_LOCAL_MODEL}
    assert mac.started == [] and mac.omlx.calls == []
