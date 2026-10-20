from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest

from llm import local_setup, local_setup_gates, local_setup_prepare, local_sources
from llm.local_model import OMLX_PIN, LocalModel


GB = 1000 ** 3
# The model CORSIA's oMLX picked for the Mac under test.
PICK = LocalModel("Jundot/Qwen3.8-27B-oQ4e-mtp", 16_971_681_558, 40960)


def _eligible(monkeypatch, model=PICK):
    """A Mac with the memory for a local model, whose oMLX already picked ``model``."""
    monkeypatch.setattr(local_setup, "local_memory_ok", lambda *_args: True)
    monkeypatch.setattr(local_setup_prepare, "picked_model", lambda: model)


def _ineligible(monkeypatch):
    """A Mac under 24 GiB: no local model."""
    monkeypatch.setattr(local_setup, "local_memory_ok", lambda *_args: False)


@pytest.fixture(autouse=True)
def fresh_flight(monkeypatch):
    """Each test sees an idle in-process flight record and no analytics writes."""
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr(local_setup, "_emit", lambda action, **properties: EMITTED.append((action, properties)))
    EMITTED.clear()


EMITTED: list[tuple[str, dict]] = []


@pytest.fixture(autouse=True)
def no_admin_key_stage(monkeypatch):
    """Registration, key lookup and the admin sign-in have their own tests."""
    async def no_key(_host, _port):
        return None

    async def secured(_server_id, _key):
        return None

    monkeypatch.setattr(local_setup, "registered_local_key", no_key)
    monkeypatch.setattr(local_setup, "own_omlx_admin_key", lambda: None)
    monkeypatch.setattr(local_setup, "secure_local_omlx", secured)
    monkeypatch.setattr(local_setup, "_omlx_supports_loopback_auth", lambda: True)
    monkeypatch.setattr(local_setup, "omlx_version", lambda: OMLX_PIN)
    monkeypatch.setattr(local_setup, "require_free_disk", lambda *_args: None)
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: False)


@pytest.fixture
def pacific_time(monkeypatch):
    """Fix machine-local rendering without leaking the process timezone."""
    previous = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    if previous is None:
        monkeypatch.delenv("TZ")
    else:
        monkeypatch.setenv("TZ", previous)
    time.tzset()


async def _finished(**kwargs) -> dict:
    """Begin a flight, wait for it, and return its terminal result (or the skip)."""
    first = await local_setup.begin_local_setup(**kwargs)
    task = local_setup._flight.task
    return await task if task is not None else first


def _status_response(**model):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"models": [{"id": "starter", "loaded": False, **model}]}

    return Response()


def test_omlx_model_without_a_reported_window_has_none():
    from llm.catalog_network_models import _parse_omlx_status

    model = _parse_omlx_status(_status_response(
        estimated_size=8_608_739_065))["starter"]

    assert model["context_length"] is None
    assert "context_window_source" not in model


def test_unreported_omlx_window_is_unavailable_missing_context_window():
    from llm.catalog_network_models import _network_model_payload, _parse_omlx_status
    from llm.model_status import _status_entry

    model = _parse_omlx_status(_status_response())["starter"]
    payload = _network_model_payload(
        "srv", {"instance_name": "This Mac"}, "omlx", model, {}, None)
    row = {
        "id": "cat-starter", "harness": "native", "backend": "network", "provider": "omlx",
        "display_name": "Starter", "description": "", "litellm_model": payload["litellm_model"],
        "context_window": payload["context_window"], "provider_model_id": "starter",
        "supports_vision": False, "supports_tools": True,
        "metadata_json": "{}", "available": True,
    }
    entry = _status_entry(row, lambda _p: True, {"srv": "online"}, lambda _s: {"starter"}, set(), set())

    assert entry["status"] == "unavailable"
    assert entry["status_reason"] == "missing_context_window"


def test_configured_starter_window_is_discovered_as_the_provider_reported_window(monkeypatch):
    """_configure_model_settings writes max_context_window; discovery reads it back."""
    from llm.catalog_network_models import _apply_admin_model, _parse_omlx_status

    saved: dict = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"models": [{"id": "starter", "thinking_default": None}]}

    async def admin_request(_cfg, method, _path, *, json=None, timeout=None):
        if method == "PUT":
            saved.update(json)
        return Response()

    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)
    asyncio.run(local_setup._configure_model_settings("server", LocalModel("org/starter", 1, 40960)))

    canonical = _parse_omlx_status(_status_response())
    assert canonical["starter"]["context_length"] is None
    _apply_admin_model(canonical, {"id": "starter", "settings": saved})

    assert canonical["starter"]["context_length"] == 40960


def test_runtime_models_window_fills_an_unreported_window():
    from llm.catalog_network_models import _enrich_runtime_windows, _parse_omlx_status

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"id": "starter", "max_context_window": 65536}]}

    sent = {}

    class Client:
        async def get(self, *_args, **kwargs):
            sent.update(kwargs)
            return Response()

    canonical = _parse_omlx_status(_status_response())
    headers = {"X-Probe": "1"}
    asyncio.run(_enrich_runtime_windows(Client(), "http://127.0.0.1:8000/v1", canonical, headers))

    assert canonical["starter"]["context_length"] == 65536
    assert sent["headers"] == headers


def test_ineligible_starter_refuses_with_subscription_recovery(monkeypatch):
    _ineligible(monkeypatch)

    result = asyncio.run(_finished())

    assert result["ok"] is False
    assert "subscription or an API key in the app" in result["message"]
    assert result["next_action"] == (
        "Run `corsia local setup` again after fixing this, or add a subscription/API key.")


def test_automatic_ineligible_starter_skips_without_weakening_safety_gate(monkeypatch):
    _ineligible(monkeypatch)

    result = asyncio.run(_finished(automatic=True))

    assert result == {
        "ok": True,
        "skipped": "ineligible",
        "message": "This computer can't run a local model well. Connect a subscription or an API key in the app.",
        "progress": [],
    }


def test_setup_uses_official_lifecycle_then_registers_downloads_and_refreshes(monkeypatch):
    events: list[str] = []
    commands: list[list[str]] = []
    _eligible(monkeypatch)
    monkeypatch.setattr(local_setup, "_ensure_omlx", lambda progress: events.append("install"))
    monkeypatch.setattr(local_setup, "_run", lambda command: commands.append(command))
    # A leftover oMLX settings file sets a non-default port; setup must use it.
    monkeypatch.setattr(local_setup, "own_omlx_port", lambda: 5200)

    async def healthy(port):
        assert port == 5200
        events.append("healthy")

    async def register(*, port=None, api_key=None):
        assert port == 5200
        events.append("register")
        return "local-server"

    async def secured(server_id, key):
        assert server_id == "local-server" and key
        events.append("secure")

    async def configure_global(server_id, _model=None):
        assert server_id == "local-server"
        events.append("global")

    async def download(server_id, progress, _model=None):
        assert server_id == "local-server"
        events.append("download")

    async def configure_model(server_id, model):
        assert (server_id, model) == ("local-server", PICK)
        events.append("model")

    async def discover(server_id):
        assert server_id == "local-server"
        events.append("discover")
        return {"ok": True}

    monkeypatch.setattr(local_setup, "_wait_for_health", healthy)
    monkeypatch.setattr(local_setup, "_register_loopback_server", register)
    monkeypatch.setattr(local_setup, "secure_local_omlx", secured)
    monkeypatch.setattr(local_setup, "_configure_global", configure_global)
    monkeypatch.setattr(local_setup, "_wait_for_download", download)
    monkeypatch.setattr(local_setup, "_configure_model_settings", configure_model)
    import llm.catalog_network_models
    import infra.state.ws
    monkeypatch.setattr(llm.catalog_network_models, "discover_network_models", discover)
    monkeypatch.setattr(infra.state.ws, "notify_models_changed", lambda: events.append("notify"))

    result = asyncio.run(_finished())

    assert commands == [["omlx", "start"]]
    assert events == [
        "notify", "install", "notify", "healthy", "register", "notify", "secure", "global",
        "notify", "download", "model", "notify", "discover", "notify",
    ]
    assert result["ok"] is True
    assert result["server_id"] == "local-server"
    assert result["progress"] == [
        "Installing", "Starting", "Configuring",
        f"Downloading {PICK.model} ({local_setup.format_size(PICK.download_size_bytes)})", "Refreshing model catalog",
    ]
    assert [action for action, _ in EMITTED] == ["started", "completed"]
    assert local_setup.setup_status() == {"state": "succeeded", "stage": None, "result": result}


@pytest.mark.parametrize("held, registered, expected", [
    ("held-key", "registered-key", "held-key"),  # oMLX signs in only with the key it holds
    (None, "registered-key", "registered-key"),
    (None, None, None),  # a new key
])
def test_setup_signs_in_with_the_key_omlx_already_holds(monkeypatch, held, registered, expected):
    keys: list[tuple[str, str]] = []
    _eligible(monkeypatch)
    monkeypatch.setattr(local_setup, "_ensure_omlx", lambda progress: None)
    monkeypatch.setattr(local_setup, "_run", lambda command: None)
    monkeypatch.setattr(local_setup, "own_omlx_port", lambda: 5200)
    monkeypatch.setattr(local_setup, "own_omlx_admin_key", lambda: held)

    async def nothing(*_args, **_kwargs):
        return None

    async def registered_key(host, port):
        assert (host, port) == (local_setup.LOCAL_HOST, 5200)
        return registered

    async def register(*, port=None, api_key=None):
        keys.append(("register", api_key))
        return "local-server"

    async def secured(server_id, key):
        keys.append(("secure", key))

    async def discover(server_id):
        return {"ok": True}

    for name in ("_wait_for_health", "_configure_global", "_wait_for_download", "_configure_model_settings"):
        monkeypatch.setattr(local_setup, name, nothing)
    monkeypatch.setattr(local_setup, "registered_local_key", registered_key)
    monkeypatch.setattr(local_setup, "_register_loopback_server", register)
    monkeypatch.setattr(local_setup, "secure_local_omlx", secured)
    import llm.catalog_network_models
    monkeypatch.setattr(llm.catalog_network_models, "discover_network_models", discover)

    result = asyncio.run(_finished())

    assert result["ok"] is True
    [(_, used), (_, signed_in)] = keys
    assert [step for step, _ in keys] == ["register", "secure"] and used == signed_in
    assert used == expected if expected else used not in (None, "", "held-key", "registered-key")


def test_download_uses_official_hf_task_endpoints(monkeypatch):
    requests: list[tuple[str, str, dict | None]] = []

    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    async def admin_request(_cfg, method, path, *, json=None, timeout=None):
        requests.append((method, path, json))
        if method == "POST":
            return Response(202, {"success": True, "task": {"task_id": "task-1"}})
        return Response(200, {"tasks": [{"task_id": "task-1", "status": "completed"}]})

    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)

    asyncio.run(local_setup._wait_for_download("local-server", lambda _stage: None, PICK))

    assert requests == [
        ("POST", "/hf/download", {"repo_id": PICK.repository, "hf_token": ""}),
        ("GET", "/hf/tasks", None),
    ]


def test_download_progress_reports_only_new_ten_percent_increments(monkeypatch):
    reports: list[str] = []
    tasks = iter([
        {"task_id": "task-1", "status": "running", "downloaded_size": 1, "total_size": 100},
        {"task_id": "task-1", "status": "running", "downloaded_size": 9, "total_size": 100},
        {"task_id": "task-1", "status": "running", "downloaded_size": 11, "total_size": 100},
        {"task_id": "task-1", "status": "running", "downloaded_size": 19, "total_size": 100},
        {"task_id": "task-1", "status": "running", "downloaded_size": 20, "total_size": 100},
        {"task_id": "task-1", "status": "completed", "downloaded_size": 100, "total_size": 100},
    ])

    class Response:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    async def admin_request(_cfg, method, _path, **_kwargs):
        if method == "POST":
            return Response({"success": True, "task": {"task_id": "task-1"}})
        return Response({"tasks": [next(tasks)]})

    async def pause(_seconds):
        return None

    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)
    monkeypatch.setattr(local_setup.asyncio, "sleep", pause)

    asyncio.run(local_setup._wait_for_download("local-server", reports.append, PICK))

    assert reports == [
        f"Downloading {PICK.model} ({local_setup.format_size(PICK.download_size_bytes)}, 10%)",
        f"Downloading {PICK.model} ({local_setup.format_size(PICK.download_size_bytes)}, 20%)",
        f"Downloading {PICK.model} ({local_setup.format_size(PICK.download_size_bytes)}, 100%)",
    ]


def test_second_request_during_a_flight_reports_its_stage_without_a_second_flight(monkeypatch):
    evaluations: list[str] = []

    def memory(*_args):
        evaluations.append("memory")
        return True

    async def subscription():
        evaluations.append("subscription")
        return None

    release = asyncio.Event()
    downloads: list[str] = []

    async def download(*_args):
        downloads.append("download")
        await release.wait()

    async def done(*_args):
        return {"ok": True}

    async def noop(*_args):
        return None

    monkeypatch.setattr(local_setup, "local_memory_ok", memory)
    monkeypatch.setattr(local_setup_prepare, "picked_model", lambda: PICK)
    monkeypatch.setattr(local_setup, "_subscription_available", subscription)
    monkeypatch.setattr(local_setup, "_ensure_omlx", lambda progress: None)
    monkeypatch.setattr(local_setup, "_run", lambda command: None)
    monkeypatch.setattr(local_setup, "_wait_for_health", noop)
    monkeypatch.setattr(local_setup, "_register_loopback_server", _server_id)
    monkeypatch.setattr(local_setup, "_configure_global", noop)
    monkeypatch.setattr(local_setup, "_wait_for_download", download)
    monkeypatch.setattr(local_setup, "_configure_model_settings", noop)
    monkeypatch.setattr("llm.catalog_network_models.discover_network_models", done)
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: None)

    async def exercise():
        first = await local_setup.begin_local_setup(no_subscription_only=True)
        task = local_setup._flight.task
        while not downloads:
            # A flight that dies before its download would otherwise spin here forever.
            assert not task.done(), task.exception()
            await asyncio.sleep(0)
        second = await local_setup.begin_local_setup(no_subscription_only=True)
        assert local_setup._flight.task is task
        assert local_setup.setup_status()["state"] == "running"
        release.set()
        await task
        return first, second

    first, second = asyncio.run(exercise())

    assert first["in_progress"] is True and first["stage"] == "Installing"
    assert second["in_progress"] is True and second["stage"].startswith("Downloading")
    assert downloads == ["download"]
    assert evaluations == ["memory", "subscription"]
    assert [action for action, _ in EMITTED] == ["started", "completed"]


async def _server_id(*, port=None, api_key=None) -> str:
    return "local-server"


def test_setup_moves_blocking_omlx_install_off_the_event_loop(monkeypatch):
    _eligible(monkeypatch)
    install_entered = threading.Event()
    allow_install = threading.Event()

    def blocking_install(_progress):
        install_entered.set()
        assert allow_install.wait(timeout=1)

    async def healthy(_port):
        return None

    async def register(*, port=None, api_key=None):
        return "local-server"

    async def configure_global(_server_id, _model=None):
        return None

    async def download(_server_id, _progress, _model=None):
        return None

    async def configure_model(_server_id, _model):
        return None

    async def discover(_server_id):
        return {"ok": True}

    monkeypatch.setattr(local_setup, "_ensure_omlx", blocking_install)
    monkeypatch.setattr(local_setup, "_run", lambda _command: None)
    monkeypatch.setattr(local_setup, "_wait_for_health", healthy)
    monkeypatch.setattr(local_setup, "_register_loopback_server", register)
    monkeypatch.setattr(local_setup, "_configure_global", configure_global)
    monkeypatch.setattr(local_setup, "_wait_for_download", download)
    monkeypatch.setattr(local_setup, "_configure_model_settings", configure_model)
    monkeypatch.setattr("llm.catalog_network_models.discover_network_models", discover)
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: None)

    async def exercise():
        response = await local_setup.begin_local_setup()
        assert response["in_progress"] is True
        assert response["stage"] == "Installing"
        task = local_setup._flight.task
        assert task is not None
        await asyncio.to_thread(install_entered.wait)
        await asyncio.wait_for(asyncio.sleep(0), timeout=0.05)
        allow_install.set()
        return await task

    assert asyncio.run(exercise())["ok"] is True


def test_terminal_setup_error_projects_as_a_distinct_source_state(monkeypatch):
    _eligible(monkeypatch)
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [])
    # The host's own oMLX install must not decide the state under test.
    monkeypatch.setattr(local_sources, "installed_omlx", lambda: "/usr/local/bin/omlx")
    failure = {
        "ok": False, "message": "oMLX failed", "next_action": "Fix oMLX, then rerun.",
    }
    local_setup._flight.result = failure

    assert local_setup.setup_status() == {"state": "failed", "stage": None, "result": failure}
    source = local_sources.omlx_source()
    assert source["state"] == "needs_setup"
    assert source["setup_error"] == "oMLX failed"
    # The payload never carries the flight's terminal hint: the fix is the app's own place.
    assert source["fix"] == local_sources.OMLX_FIX
    assert "setup_next_action" not in source


def test_a_new_request_clears_the_previous_failure(monkeypatch):
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [])
    _ineligible(monkeypatch)
    local_setup._flight.result = {"ok": False, "message": "oMLX failed"}

    skipped = asyncio.run(local_setup.begin_local_setup(automatic=True))

    assert skipped["skipped"] == "ineligible"
    assert local_setup.setup_status()["state"] == "idle"
    assert "setup_error" not in local_sources.omlx_source()


def test_ready_managed_server_overrides_a_terminal_setup_error(monkeypatch):
    _eligible(monkeypatch)
    local_setup._flight.result = {
        "ok": False, "message": "oMLX failed", "next_action": "Fix oMLX, then rerun.",
    }
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [{
        "id": "managed", "kind": "managed", "status": "online", "updated_at": "now",
    }])

    source = local_sources.omlx_source(({"omlx@managed"}, set()))

    assert source["state"] == "ready"
    assert "setup_error" not in source


def test_server_setup_keeps_automatic_ineligible_semantics(monkeypatch):
    _ineligible(monkeypatch)

    result = asyncio.run(local_setup.begin_local_setup(automatic=True))

    assert local_setup._flight.task is None
    assert result["ok"] is True and result["skipped"] == "ineligible"
    assert EMITTED == [("skipped", {"reason": "ineligible"})]


def test_server_setup_keeps_if_no_subscription_semantics(monkeypatch):
    _eligible(monkeypatch)
    async def available():
        return local_setup_gates.SubscriptionSource("codex", "steve@example.com")

    monkeypatch.setattr(local_setup, "_subscription_available", available)

    result = asyncio.run(local_setup.begin_local_setup(no_subscription_only=True))

    assert local_setup._flight.task is None
    assert result["ok"] is True and result["skipped"] == "subscription_available"
    assert result["message"] == "Codex is signed in as steve@example.com, so the local model is skipped. You can add one from the app."
    assert EMITTED == [("skipped", {"reason": "subscription_available"})]


def _harness_rows(monkeypatch, rows, auth_state=None):
    from connections import claude_discovery, models

    probed = []

    async def auth_status(binary):
        probed.append(binary)
        return auth_state

    monkeypatch.setattr(models, "get_connection", lambda provider, db=None: rows.get(provider))
    monkeypatch.setattr(claude_discovery, "auth_status_state", auth_status)
    return probed


_UNCHECKED_CLAUDE = {"status": "available", "config": {"binary": "/bin/claude"}}


@pytest.mark.parametrize("auth_state", ["unverified", "needs_auth", "ready"])
def test_an_unchecked_installed_claude_code_is_not_a_working_provider(monkeypatch, auth_state):
    probed = _harness_rows(monkeypatch, {"claude-code": _UNCHECKED_CLAUDE}, auth_state)

    assert asyncio.run(local_setup._subscription_available()) is None
    assert probed == []


@pytest.mark.parametrize("state", ["needs_auth", "not_installed"])
def test_a_source_now_signed_out_does_not_count_whatever_it_was(monkeypatch, state):
    row = {"status": "needs_auth", "config": {"source_check": {
        "state": state, "prior_state": "ready", "reason": "Not signed in", "checked_at": "t"}}}
    _harness_rows(monkeypatch, {"claude-code": row, "codex": row})

    assert asyncio.run(local_setup._subscription_available()) is None


def test_a_ready_source_with_an_active_limit_does_not_count_even_via_prior_ready(monkeypatch):
    row = {"status": "available", "config": {"source_check": {
        "state": "unverified", "prior_state": "ready", "reason": "t", "checked_at": "t"}}}
    _harness_rows(monkeypatch, {"codex": row})
    monkeypatch.setattr(local_setup_gates, "_default_limit", lambda _p: {"resets_at": None})

    assert asyncio.run(local_setup._subscription_available()) is None


def test_a_checked_ready_source_counts(monkeypatch):
    row = {"status": "ready", "config": {"source_check": {
        "state": "ready", "prior_state": None, "reason": None, "checked_at": "t"}}}
    _harness_rows(monkeypatch, {"claude-code": row})

    source = asyncio.run(local_setup._subscription_available())
    assert source.provider == "claude-code"


def test_still_usable_unverified_row_counts_without_a_probe(monkeypatch):
    still_ready = {"status": "available", "config": {"source_check": {
        "state": "unverified", "prior_state": "ready", "reason": "Check timed out", "checked_at": "t"}}}
    probed = _harness_rows(monkeypatch, {"codex": still_ready})

    source = asyncio.run(local_setup._subscription_available())
    assert source == local_setup_gates.SubscriptionSource("codex", None)
    assert probed == []


def test_checked_signed_out_harnesses_are_no_subscription(monkeypatch):
    signed_out = {"status": "needs_auth", "config": {"source_check": {
        "state": "needs_auth", "prior_state": None, "reason": "Not signed in", "checked_at": "t"}}}
    probed = _harness_rows(monkeypatch, {"claude-code": signed_out, "codex": signed_out})

    assert asyncio.run(local_setup._subscription_available()) is None
    assert probed == []


def test_known_default_account_limit_is_not_a_usable_subscription(monkeypatch, pacific_time):
    ready = {"status": "ready", "config": {"source_check": {
        "state": "ready", "prior_state": None, "reason": None, "checked_at": "t"}}}
    signed_out = {"status": "needs_auth", "config": {"source_check": {
        "state": "needs_auth", "prior_state": None, "reason": "Not signed in", "checked_at": "t"}}}
    _harness_rows(monkeypatch, {"claude-code": ready, "codex": signed_out})
    monkeypatch.setattr(local_setup_gates, "_default_limit", lambda provider: {
        "resets_at": "2026-10-09T05:00:00+00:00"} if provider == "claude-code" else None)

    assert asyncio.run(local_setup._subscription_available()) is None
    assert asyncio.run(local_setup._limited_subscription_message()) == (
        "Claude Code is signed in but has reached its plan limit; it resets Thu Oct 8, 10:00 PM PDT."
    )


def test_unknown_usage_does_not_claim_a_subscription_is_limited(monkeypatch):
    unknown = {"status": "unverified", "config": {"source_check": {
        "state": "unverified", "prior_state": None, "reason": "Check timed out", "checked_at": "t"}}}
    _harness_rows(monkeypatch, {"claude-code": unknown, "codex": unknown})
    monkeypatch.setattr(local_setup_gates, "_default_limit", lambda _provider: {
        "resets_at": "2099-01-01T12:00:00+00:00"})

    assert asyncio.run(local_setup._subscription_available()) is None
    assert asyncio.run(local_setup._limited_subscription_message()) is None


def _failing_setup(monkeypatch, failing_step, exc):
    _eligible(monkeypatch)
    monkeypatch.setattr(local_setup, "_ensure_omlx", lambda progress: None)
    monkeypatch.setattr(local_setup, "_run", lambda command: None)

    async def healthy(_port):
        return None

    async def register(*, port=None, api_key=None):
        if failing_step == "register":
            raise exc
        return "local-server"

    async def configure_global(_server_id, _model=None):
        return None

    async def download(_server_id, _progress, _model=None):
        raise exc

    monkeypatch.setattr(local_setup, "_wait_for_health", healthy)
    monkeypatch.setattr(local_setup, "_register_loopback_server", register)
    monkeypatch.setattr(local_setup, "_configure_global", configure_global)
    monkeypatch.setattr(local_setup, "_wait_for_download", download)
    import infra.state.ws
    monkeypatch.setattr(infra.state.ws, "notify_models_changed", lambda: None)


def test_registration_http_exception_is_saved_with_its_detail(monkeypatch, caplog):
    from fastapi import HTTPException

    _failing_setup(monkeypatch, "register", HTTPException(502, "oMLX refused the registration"))

    result = asyncio.run(_finished())

    assert result["ok"] is False
    assert result["message"] == "oMLX refused the registration"
    assert local_setup.setup_failure()["message"] == "oMLX refused the registration"
    assert "Local setup failed" in caplog.text


def test_unreadable_omlx_payload_is_saved_as_a_readable_failure(monkeypatch):
    _failing_setup(monkeypatch, "download", ValueError("Expecting value: line 1 column 1"))

    result = asyncio.run(_finished())

    assert result["ok"] is False
    assert result["message"] == "oMLX returned an unreadable response."
    assert local_setup.setup_failure()["message"] == "oMLX returned an unreadable response."


def test_unexpected_error_is_saved_and_a_new_request_may_start(monkeypatch):
    _failing_setup(monkeypatch, "download", KeyError("boom"))

    result = asyncio.run(_finished())

    assert result["ok"] is False
    assert "KeyError" in result["message"]
    assert local_setup.setup_status()["state"] == "failed"
    assert [action for action, _ in EMITTED] == ["started", "failed"]
    asyncio.run(_finished())
    assert local_setup.setup_status()["state"] == "failed"


def test_server_setup_task_failure_is_observed(caplog):
    async def fail():
        raise OSError("disk gone")

    async def run():
        task = asyncio.create_task(fail())
        task.add_done_callback(local_setup._observe_setup_task)
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(run())

    assert "Local setup task ended with an error" in caplog.text


def test_local_source_reports_the_picked_models_size_and_window(monkeypatch):
    _eligible(monkeypatch)
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [])

    source = local_sources.omlx_source()
    assert (source["download_size"], source["context_window"]) == ("15.81 GiB", 40960)
    # Until oMLX picks, there is no model to size.
    _eligible(monkeypatch, model=None)
    source = local_sources.omlx_source()
    assert source["eligible"] is True and source["download_size"] is None and source["context_window"] is None


def test_local_source_sends_the_download_percent_as_a_number(monkeypatch):
    _eligible(monkeypatch)
    monkeypatch.setattr(local_setup, "setup_stage", lambda: "Downloading Starter (8.60 GiB, 40%)")
    monkeypatch.setattr(local_setup, "setup_status", lambda: {"state": "running"})
    monkeypatch.setattr(local_setup, "download_estimate", lambda: (600, False))
    source = local_sources.omlx_source()
    assert source["download_percent"] == 40 and source["setup_stage"].startswith("Downloading")
    assert source["download_eta_seconds"] == 600 and source["download_stalled"] is False
    for stage, percent in (("Installing", None), ("Downloading Starter (8.60 GiB, 100%)", 100), ("odd 250%", 100)):
        monkeypatch.setattr(local_setup, "setup_stage", lambda stage=stage: stage)
        assert local_sources.omlx_source()["download_percent"] == percent


def test_download_progress_estimates_only_after_measured_progress_and_marks_a_stall():
    from llm.local_setup_results import DownloadProgress

    progress = DownloadProgress(0)
    progress.observe(100, 1_000, 0)
    assert progress.eta_seconds is None and progress.stalled is False
    progress.observe(300, 1_000, 10)
    assert progress.eta_seconds == 35 and progress.stalled is False
    progress.observe(300, 1_000, 41)
    assert progress.eta_seconds is None and progress.stalled is True
    # Polling continues through a pause; new transfer progress clears it without a retry.
    progress.observe(500, 1_000, 51)
    assert progress.eta_seconds == 25 and progress.stalled is False


def test_cancelled_local_setup_is_a_successful_terminal_noop_for_the_source(monkeypatch):
    async def run():
        await asyncio.Event().wait()

    async def scenario():
        task = asyncio.create_task(run())
        local_setup._flight.task = task
        await asyncio.sleep(0)
        result = await local_setup.cancel_local_setup()
        return result, task.cancelled()

    monkeypatch.setattr(local_setup.local_setup_operation, "read", lambda: None)
    monkeypatch.setattr(local_setup.local_setup_operation, "clear", lambda: None)
    monkeypatch.setattr(local_setup, "_notify_models_changed", lambda: None)
    result, cancelled = asyncio.run(scenario())

    assert result == {"ok": True, "cancelled": True}
    assert local_setup._flight.result == {"ok": True, "cancelled": True}
    assert cancelled


@pytest.mark.parametrize(("servers", "membership", "code"), [
    ([{"id": "s1", "status": "offline", "kind": "omlx"}], (set(), set()), "not_running"),
    ([{"id": "s1", "status": "online", "kind": "omlx"}], (set(), set()), "no_models"),
    ([{"id": "s1", "status": "online", "kind": "omlx"}], ({"omlx@s1"}, set()), None),
])
def test_local_source_names_why_it_is_not_ready_as_a_code(monkeypatch, servers, membership, code):
    _eligible(monkeypatch)
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: servers)
    source = local_sources.omlx_source(membership)
    assert source.get("reason_code") == code


def test_a_setup_failure_or_stopped_download_replaces_the_reason_and_its_code(monkeypatch):
    _eligible(monkeypatch)
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [{"id": "s1", "status": "offline", "kind": "omlx"}])
    monkeypatch.setattr(local_setup, "setup_failure", lambda: {"ok": False, "message": "No space left", "next_action": "Free space"})
    source = local_sources.omlx_source((set(), set()))
    assert source["reason"] == "No space left" and "reason_code" not in source
