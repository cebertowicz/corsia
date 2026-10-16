"""Automatic local setup respects an existing oMLX, and a long download is not a failure."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess

import pytest

from llm import (
    local_setup, local_setup_existing, local_setup_operation, local_setup_ownership, local_setup_prepare,
    local_sources,
)
from llm.local_model import OMLX_PIN, LocalModel
from vendor_account_fixtures import isolated_shutdown_tasks  # noqa: F401

# The model CORSIA's own oMLX picked for the Mac under test.
PICK = LocalModel("Jundot/Qwen3.8-27B-oQ4e-mtp", 16_971_681_558, 40960)
STARTER = local_setup_existing.OWN_OMLX_STARTER


@pytest.fixture(autouse=True)
def fresh_flight(monkeypatch):
    local_setup_operation.clear()
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr(local_setup, "_emit", lambda action, **properties: None)
    monkeypatch.setattr(local_setup, "local_memory_ok", lambda *_args: True)
    monkeypatch.setattr(local_setup_prepare, "picked_model", lambda: PICK)
    monkeypatch.setattr(local_setup, "omlx_version", lambda: OMLX_PIN)
    monkeypatch.setattr("llm.local_setup_auth.omlx_version", lambda: "0.7.0")
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: None)


@pytest.fixture
def calls(monkeypatch):
    """Record every write the flight could make against oMLX; mock the rest."""
    class Recorded(list):
        ports: list[int]

    recorded = Recorded()
    ports: list[int] = []

    async def healthy(port=8000):
        recorded.append("health")
        ports.append(port)

    async def register(*, port=8000, api_key=None):
        recorded.append("register")
        ports.append(port)
        return "local-server"

    async def no_key(host, port):
        return None

    async def secure(server_id, key):
        recorded.append("secure")

    async def global_settings(server_id, _model=None):
        recorded.append("global_settings")

    async def model_settings(server_id, _model):
        recorded.append("model_settings")

    async def download(server_id, progress, _model=None):
        recorded.append("download")

    async def discover(server_id):
        recorded.append("discover")
        return {"ok": True}

    monkeypatch.setattr(local_setup, "_ensure_omlx", lambda progress: recorded.append("install"))
    monkeypatch.setattr(local_setup, "_omlx_supports_loopback_auth", lambda: True)
    monkeypatch.setattr(local_setup, "_run", lambda command: recorded.append(" ".join(command)))
    monkeypatch.setattr(local_setup, "_wait_for_health", healthy)
    monkeypatch.setattr(local_setup, "_register_loopback_server", register)
    monkeypatch.setattr(local_setup, "registered_local_key", no_key)
    monkeypatch.setattr(local_setup, "secure_local_omlx", secure)
    monkeypatch.setattr(local_setup, "_configure_global", global_settings)
    monkeypatch.setattr(local_setup, "_configure_model_settings", model_settings)
    monkeypatch.setattr(local_setup, "_wait_for_download", download)
    monkeypatch.setattr("llm.catalog_network_models.discover_network_models", discover)
    recorded.ports = ports
    return recorded


def _models(monkeypatch, rows):
    monkeypatch.setattr("llm.catalog.list_models", lambda **_kwargs: rows)

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr("infra.db_async.run_in_db", inline)


async def _finished(**kwargs) -> dict:
    first = await local_setup.begin_local_setup(**kwargs)
    task = local_setup._flight.task
    return await task if task is not None else first


def test_a_preexisting_omlx_with_models_is_registered_and_never_written(monkeypatch, calls):
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: True)
    _models(monkeypatch, [{"litellm_model": "omlx@local-server/my-model"},
                          {"litellm_model": "omlx@other/elsewhere"}])

    result = asyncio.run(_finished(automatic=True))

    assert calls == ["health", "register", "discover"]
    assert result["ok"] is True and result["existing"] is True and result["models"] == 1
    assert result["server_id"] == "local-server"
    assert "advice" not in result
    assert local_setup.setup_status()["state"] == "succeeded"


def test_a_preexisting_omlx_on_another_port_is_found_at_its_own_port(monkeypatch, tmp_path, calls):
    home = tmp_path / "home"
    (home / ".omlx").mkdir(parents=True)
    settings = home / ".omlx" / "settings.json"
    settings.write_text(json.dumps({"server": {"port": 5200}, "auth": {"api_key": "sk-secret-value"}}))
    before = settings.read_bytes()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: True)
    _models(monkeypatch, [{"litellm_model": "omlx@local-server/my-model"}] * 3)

    result = asyncio.run(_finished(automatic=True))

    assert calls.ports == [5200, 5200]
    assert calls == ["health", "register", "discover"]
    assert result["ok"] is True and result["models"] == 3
    assert "sk-secret-value" not in json.dumps(result)
    assert settings.read_bytes() == before


def test_the_starter_download_into_the_persons_omlx_uses_its_port_too(monkeypatch, tmp_path, calls):
    home = tmp_path / "home"
    (home / ".omlx").mkdir(parents=True)
    (home / ".omlx" / "settings.json").write_text(json.dumps({"server": {"port": 5200}}))
    monkeypatch.setenv("HOME", str(home))

    asyncio.run(local_setup_existing.download_into_own_omlx(lambda stage: None, [], STARTER))

    assert calls.ports == [5200, 5200]


@pytest.mark.parametrize("content", [None, "not json", "[]", json.dumps({"server": 7}),
                                      json.dumps({"server": {"port": "5200"}}),
                                      json.dumps({"server": {"port": True}}),
                                      json.dumps({"server": {"port": 70000}}),
                                      json.dumps({})])
def test_an_unreadable_or_portless_settings_file_falls_back_to_8000(tmp_path, content):
    settings = tmp_path / "settings.json"
    if content is not None:
        settings.write_text(content)

    assert local_setup_ownership.own_omlx_port(settings) == 8000


def test_the_settings_file_follows_the_data_root_the_omlx_app_moved(monkeypatch, tmp_path):
    home = tmp_path / "home"
    moved = tmp_path / "elsewhere"
    moved.mkdir()
    (moved / "settings.json").write_text(json.dumps({"server": {"port": 5300}}))
    bootstrap = home / "Library" / "Application Support" / "oMLX"
    bootstrap.mkdir(parents=True)
    (bootstrap / "base-path").write_text(f"{moved}\n")
    (home / ".omlx").mkdir()
    (home / ".omlx" / "settings.json").write_text(json.dumps({"server": {"port": 5200}}))
    monkeypatch.setenv("HOME", str(home))

    assert local_setup_ownership.own_omlx_settings_file() == moved / "settings.json"
    assert local_setup_ownership.own_omlx_port() == 5300


def test_without_a_moved_data_root_the_settings_file_is_under_dot_omlx(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    assert local_setup_ownership.own_omlx_settings_file() == tmp_path / ".omlx" / "settings.json"


def test_reading_the_port_returns_only_the_port_and_logs_nothing(tmp_path, caplog):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"server": {"port": 5200}, "auth": {"api_key": "sk-secret-value"}}))
    before = settings.read_bytes()
    caplog.set_level("DEBUG")

    port = local_setup_ownership.own_omlx_port(settings)

    assert port == 5200 and type(port) is int
    assert "sk-secret-value" not in caplog.text and caplog.text == ""
    assert settings.read_bytes() == before


def test_the_admin_key_omlx_holds_is_read_from_its_settings_file_and_nothing_else(tmp_path, caplog):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"server": {"port": 5200}, "auth": {"api_key": "held-key"}}))
    before = settings.read_bytes()
    caplog.set_level("DEBUG")

    assert local_setup_ownership.own_omlx_admin_key(settings) == "held-key"
    assert caplog.text == "" and settings.read_bytes() == before


@pytest.mark.parametrize("content", [None, "not json", "[]", json.dumps({}), json.dumps({"auth": 7}),
                                      json.dumps({"auth": {}}), json.dumps({"auth": {"api_key": None}}),
                                      json.dumps({"auth": {"api_key": ""}}),
                                      json.dumps({"auth": {"api_key": 12345}})])
def test_no_usable_admin_key_in_the_settings_file_reads_as_none(tmp_path, content):
    settings = tmp_path / "settings.json"
    if content is not None:
        settings.write_text(content)

    assert local_setup_ownership.own_omlx_admin_key(settings) is None


def test_the_admin_key_follows_the_data_root_the_omlx_app_moved(monkeypatch, tmp_path):
    moved = tmp_path / "elsewhere"
    moved.mkdir()
    (moved / "settings.json").write_text(json.dumps({"auth": {"api_key": "moved-key"}}))
    bootstrap = tmp_path / "Library" / "Application Support" / "oMLX"
    bootstrap.mkdir(parents=True)
    (bootstrap / "base-path").write_text(f"{moved}\n")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert local_setup_ownership.own_omlx_admin_key() == "moved-key"


def test_a_preexisting_omlx_that_is_not_running_is_left_alone(monkeypatch, calls):
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: True)

    async def down(port=8000):
        calls.append("health")
        raise RuntimeError("oMLX did not become healthy.")

    monkeypatch.setattr(local_setup, "_wait_for_health", down)

    result = asyncio.run(_finished(automatic=True))

    assert calls == ["health"]
    assert result["ok"] is True and result["skipped"] == "existing_omlx_not_running"


def test_a_preexisting_omlx_with_no_models_gets_no_starter_download(monkeypatch, calls):
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: True)
    _models(monkeypatch, [])

    result = asyncio.run(_finished(automatic=True))

    assert "download" not in calls and "global_settings" not in calls and "model_settings" not in calls
    assert result["ok"] is True and result["skipped"] == "existing_omlx_no_models"


def test_a_fresh_install_still_configures_downloads_and_refreshes(monkeypatch, calls):
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: False)

    result = asyncio.run(_finished(automatic=True))

    assert calls == [
        "install", "omlx start", "health", "register", "secure", "global_settings",
        "download", "model_settings", "discover",
    ]
    assert result["ok"] is True and result["model"] == PICK.model


def test_an_explicit_setup_adopts_an_existing_install_without_configuring_it(monkeypatch, calls):
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: True)
    _models(monkeypatch, [{"litellm_model": "omlx@local-server/my-model"}])

    result = asyncio.run(_finished())

    assert calls == ["health", "register", "discover"]
    assert result["ok"] is True and result["existing"] is True


def test_setup_records_that_it_installed_omlx_and_only_then_owns_it(monkeypatch, tmp_path):
    marker = tmp_path / "marker"
    monkeypatch.setattr(local_setup_ownership, "marker_path", lambda: marker)
    monkeypatch.setattr(local_setup_ownership, "run_command", lambda command: None)
    monkeypatch.setattr(local_setup_ownership.shutil, "which",
                        lambda name: "/opt/bin/brew" if name == "brew" else None)

    assert local_setup_ownership.existing_install_is_foreign() is False  # nothing installed
    local_setup_ownership.ensure_omlx(lambda stage: None)
    assert marker.exists()

    monkeypatch.setattr(local_setup_ownership.shutil, "which", lambda name: "/opt/bin/omlx")
    monkeypatch.setattr(local_setup_ownership, "setup_owns_current_omlx", lambda *_args: True)
    assert local_setup_ownership.existing_install_is_foreign() is False  # ours: marker exists
    monkeypatch.setattr(local_setup_ownership, "setup_owns_current_omlx", lambda *_args: False)
    local_setup_ownership._foreign_cache.clear()  # the stubbed brew answer changed, not the key
    assert local_setup_ownership.existing_install_is_foreign() is True  # stale marker is foreign
    marker.unlink()
    assert local_setup_ownership.existing_install_is_foreign() is True  # installed, no marker


def test_foreign_check_is_cached_until_the_marker_or_binary_changes(monkeypatch, tmp_path):
    marker = tmp_path / "marker"
    marker.write_text("x")
    omlx = ["/opt/bin/omlx"]
    brew_calls = []
    monkeypatch.setattr(local_setup_ownership, "marker_path", lambda: marker)
    monkeypatch.setattr(local_setup_ownership.shutil, "which",
                        lambda name: "/opt/bin/brew" if name == "brew" else omlx[0])
    monkeypatch.setattr(local_setup_ownership, "setup_owns_current_omlx",
                        lambda *args: brew_calls.append(args) or True)

    assert local_setup_ownership.existing_install_is_foreign() is False
    assert local_setup_ownership.existing_install_is_foreign() is False
    assert len(brew_calls) == 1

    omlx[0] = "/other/omlx"
    local_setup_ownership.existing_install_is_foreign()
    assert len(brew_calls) == 2

    os.utime(marker, ns=(1, 1))
    local_setup_ownership.existing_install_is_foreign()
    assert len(brew_calls) == 3


def test_a_failed_brew_read_is_foreign_for_that_read_only(monkeypatch, tmp_path):
    marker = tmp_path / "marker"
    marker.write_text("x")
    keg = tmp_path / "keg"
    monkeypatch.setattr(local_setup_ownership, "marker_path", lambda: marker)
    monkeypatch.setattr(local_setup_ownership.shutil, "which",
                        lambda name: "/opt/bin/brew" if name == "brew" else str(keg / "bin" / "omlx"))
    failures = [subprocess.TimeoutExpired("brew", 30)]

    def brew(command):
        if failures:
            raise failures.pop()
        return "omlx 0.7.0" if "list" in command else str(keg)

    monkeypatch.setattr(local_setup_ownership, "command_output", brew)

    assert local_setup_ownership.existing_install_is_foreign() is True  # brew timed out
    assert local_setup_ownership.existing_install_is_foreign() is False  # asked again: setup's own


def test_a_cache_cleared_by_another_thread_mid_read_still_answers(monkeypatch, tmp_path):
    marker = tmp_path / "marker"
    marker.write_text("x")
    monkeypatch.setattr(local_setup_ownership, "marker_path", lambda: marker)
    monkeypatch.setattr(local_setup_ownership.shutil, "which",
                        lambda name: "/opt/bin/brew" if name == "brew" else "/opt/bin/omlx")
    monkeypatch.setattr(local_setup_ownership, "setup_owns_current_omlx", lambda *_args: True)

    class ClearedByAnotherReader(dict):
        """Every lookup is followed by another thread's clear for a different key."""

        def __contains__(self, key):
            found = super().__contains__(key)
            self.clear()
            return found

        def get(self, key, default=None):
            value = super().get(key, default)
            self.clear()
            return value

    cache = ClearedByAnotherReader()
    monkeypatch.setattr(local_setup_ownership, "_foreign_cache", cache)
    assert local_setup_ownership.existing_install_is_foreign() is False  # fills the cache
    assert local_setup_ownership.existing_install_is_foreign() is False  # cleared mid-read, no KeyError


def test_an_install_that_was_already_there_is_not_marked(monkeypatch, tmp_path):
    marker = tmp_path / "marker"
    monkeypatch.setattr(local_setup_ownership, "marker_path", lambda: marker)
    monkeypatch.setattr(local_setup_ownership.shutil, "which", lambda name: "/opt/bin/omlx")

    local_setup_ownership.ensure_omlx(lambda stage: None)

    assert not marker.exists()
    assert local_setup_ownership.existing_install_is_foreign() is True


class _Download:
    """A fake oMLX HF task endpoint serving one task through the given statuses."""

    def __init__(self, statuses, error=None):
        self.statuses = iter(statuses)
        self.error = error

    async def request(self, _cfg, method, _path, **_kwargs):
        class Response:
            def __init__(self, status_code, payload):
                self.status_code, self._payload = status_code, payload

            def json(self):
                return self._payload

        if method == "POST":
            return Response(202, {"success": True, "task": {"task_id": "task-1"}})
        task = {"task_id": "task-1", "status": next(self.statuses)}
        if self.error and task["status"] == "failed":
            task["error"] = self.error
        return Response(200, {"tasks": [task]})


def _expired_download(monkeypatch, calls, statuses, error=None):
    """Wire the real download wait with a zero-length foreground wait."""
    seen: list[dict] = []
    fake = _Download(statuses, error)
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: False)
    monkeypatch.setattr(local_setup, "DOWNLOAD_WAIT_SECONDS", 0)
    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", fake.request)

    async def pause(seconds):
        seen.append({"seconds": seconds, **local_setup.setup_status()})

    monkeypatch.setattr(local_setup.asyncio, "sleep", pause)
    monkeypatch.setattr(local_setup, "_wait_for_download", _real_wait)
    return seen


_real_wait = local_setup._wait_for_download


def test_an_expired_wait_keeps_following_then_finishes_configured(monkeypatch, calls):
    seen = _expired_download(monkeypatch, calls, ["running", "running", "completed"])

    result = asyncio.run(_finished(automatic=True))

    assert [entry["state"] for entry in seen] == ["downloading", "downloading"]
    assert {entry["seconds"] for entry in seen} == {local_setup.DOWNLOAD_BACKGROUND_POLL_SECONDS}
    assert "Still downloading" in seen[0]["stage"]
    assert calls[-2:] == ["model_settings", "discover"]  # configured and catalog refreshed
    assert result["ok"] is True and result["model"] == PICK.model
    assert local_setup.setup_status()["state"] == "succeeded"
    assert local_setup.setup_failure() is None and local_setup._flight.following is False


def test_an_expired_wait_then_a_failed_download_is_a_truthful_failure(monkeypatch, calls):
    _expired_download(monkeypatch, calls, ["running", "failed"], error="disk full")

    result = asyncio.run(_finished(automatic=True))

    assert result["ok"] is False and result["message"] == "disk full"
    assert "model_settings" not in calls
    assert local_setup.setup_status()["state"] == "failed"


def test_a_download_outliving_the_follow_bound_stops_following_without_failing(monkeypatch, calls):
    _expired_download(monkeypatch, calls, ["running"] * 3)
    monkeypatch.setattr(local_setup, "DOWNLOAD_FOLLOW_SECONDS", 0)
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [])
    monkeypatch.setattr(local_sources, "installed_omlx", lambda: None)

    result = asyncio.run(_finished(automatic=True))

    assert result["ok"] is True and result["downloading"] is True
    assert "model_settings" not in calls
    status = local_setup.setup_status()
    assert status["state"] == "downloading" and status["result"] is result
    assert local_setup.setup_failure() is None
    source = local_sources.omlx_source(membership=(set(), set()))
    assert source["setup_downloading"] is True and "setup_error" not in source
    assert source["reason"] == result["message"]


def test_the_source_row_marks_a_running_background_download(monkeypatch, calls):
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [])

    async def scenario():
        local_setup._flight.task = asyncio.create_task(asyncio.sleep(10))
        local_setup._flight.stage = "Still downloading the starter"
        local_setup._flight.following = True
        try:
            return local_sources.omlx_source(membership=(set(), set()))
        finally:
            local_setup._flight.task.cancel()

    source = asyncio.run(scenario())

    assert source["state"] == "needs_setup" and source["setup_downloading"] is True
    assert "setup_error" not in source


def test_shutdown_cancels_and_joins_the_setup_flight(monkeypatch, isolated_shutdown_tasks):
    # Shutdown joins every lifecycle task in process state. After a test that ran the session
    # app they sit on that app's loop, so this test owns the registry, as the other shutdown tests do.
    from unittest.mock import Mock

    from app_shutdown_owned_tasks import shutdown_owned_tasks
    from connections import account_controls

    async def no_signins():
        return None

    async def scenario():
        monkeypatch.setattr(account_controls, "stop_all_signins", no_signins)
        local_setup._flight.task = asyncio.create_task(asyncio.sleep(60))
        await asyncio.sleep(0)
        await shutdown_owned_tasks([None] * 5, Mock())
        return local_setup._flight.task

    assert asyncio.run(scenario()).cancelled()


async def _add_starter() -> tuple[dict, dict | None]:
    first = await local_setup.begin_add_starter_model()
    task = local_setup._flight.task
    return first, (await task if task is not None else None)


def test_add_starter_model_downloads_into_your_omlx_and_writes_no_settings(monkeypatch, calls):
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: True)
    notified: list[str] = []
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: notified.append("models"))

    first, result = asyncio.run(_add_starter())

    assert first["ok"] is True
    assert calls == ["health", "register", "download", "discover"]
    assert "global_settings" not in calls and "model_settings" not in calls and "install" not in calls
    assert result["ok"] is True and result["model"] == STARTER.model
    assert local_setup.setup_status()["state"] == "succeeded"
    assert "models" in notified


def test_add_starter_model_refuses_an_omlx_corsia_installed(monkeypatch, calls):
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: False)

    first, result = asyncio.run(_add_starter())

    assert first["ok"] is False and first["reason"] == "not_your_omlx"
    assert result is None and calls == []


def test_add_starter_model_refuses_a_mac_that_cannot_run_it(monkeypatch, calls):
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: True)
    monkeypatch.setattr(local_setup, "local_memory_ok", lambda *_args: False)

    first, result = asyncio.run(_add_starter())

    assert first == {"ok": False, "reason": "mac_cannot_run",
                     "message": "This Mac can't run a local model well."}
    assert result is None and calls == []


def test_add_starter_model_reports_a_failed_download_and_writes_nothing(monkeypatch, calls):
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: True)

    async def failing(server_id, progress, _model=None):
        calls.append("download")
        raise RuntimeError("disk full")

    monkeypatch.setattr(local_setup, "_wait_for_download", failing)

    first, result = asyncio.run(_add_starter())

    assert first["ok"] is True
    assert result["ok"] is False and result["message"] == "disk full"
    assert "global_settings" not in calls and "model_settings" not in calls
    assert local_setup.setup_status()["state"] == "failed"


def test_add_starter_model_while_a_flight_runs_reports_that_flight(monkeypatch, calls):
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: True)

    async def scenario():
        gate = asyncio.Event()

        async def slow(server_id, progress, _model=None):
            await gate.wait()

        monkeypatch.setattr(local_setup, "_wait_for_download", slow)
        first = await local_setup.begin_add_starter_model()
        second = await local_setup.begin_add_starter_model()
        gate.set()
        await local_setup._flight.task
        return first, second

    first, second = asyncio.run(scenario())

    assert first["ok"] is True and second["ok"] is True
    assert calls.count("register") == 1
