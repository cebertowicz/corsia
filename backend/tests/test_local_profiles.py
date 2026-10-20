"""Local setup's model-independent settings, disk check, download wiring and routes.

The model itself is oMLX's recommender pick (``test_local_model.py``). Everything
is mocked: no model is loaded or downloaded and no disk is read.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections import namedtuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm import local_profiles, local_setup, local_setup_operation, local_setup_flow as flow, local_setup_runtime as runtime, local_setup_download as downloads
from llm.local_model import LocalModel

GIB = 1024**3
MODEL = LocalModel("Jundot/Qwen3.8-27B-oQ4e-mtp", 16_971_681_558, 262_144)
BIG = LocalModel("Jundot/Qwen3.8-Flash-Next-oQ4e-mtp", 106_294_663_060, 262_144)


@pytest.fixture(autouse=True)
def fresh_flight(monkeypatch):
    local_setup_operation.clear()
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr(local_setup, "_emit", lambda action, **properties: None)
    # Each test starts with an unclaimed local setup flight.
    yield
    local_setup_operation.clear()




def test_local_setup_view_reads_ram_once_per_process(monkeypatch):
    from llm import local_setup_gates, local_setup_prepare

    reads = []
    monkeypatch.setattr("infra.host_resources.host_total_ram_bytes", lambda: reads.append(True) or 128 * GIB)
    local_setup_gates.host_ram_bytes.cache_clear()
    # Each test starts with an unclaimed local setup flight.
    monkeypatch.setattr("llm.local_model.set_up_model", lambda: None)
    monkeypatch.setattr(local_setup_prepare, "_prepare", local_setup_prepare._Prepare())

    try:
        assert asyncio.run(local_setup_prepare.pick_view())["state"] == "not_checked"
        assert asyncio.run(local_setup_prepare.pick_view())["state"] == "not_checked"
        assert reads == [True]
    finally:
        local_setup_gates.host_ram_bytes.cache_clear()


def test_download_and_settings_use_the_picked_model(monkeypatch):
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"success": True, "task": {"task_id": "t1"}, "tasks": [{"task_id": "t1", "status": "completed"}],
                    "models": [{"id": BIG.model, "thinking_default": True}]}

    async def admin_request(_cfg, method, path, *, json=None, timeout=None):
        calls.append((method, path, json))
        return Response()

    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)
    asyncio.run(local_setup._wait_for_download("srv", lambda _m: None, BIG))
    asyncio.run(runtime.configure_model_settings("srv", BIG))

    assert calls[0] == ("POST", "/hf/download", {"repo_id": BIG.repository, "hf_token": ""})
    method, path, payload = calls[-1]
    assert (method, path) == ("PUT", f"/models/{BIG.model}/settings")
    assert payload == {**local_profiles.MODEL_SETTINGS, "max_context_window": 262144, "mtp_enabled": True}


REPO = "org/model"
usage = namedtuple("usage", "total used free")


def test_disk_check_fails_with_the_space_needed_and_passes_with_room(monkeypatch, tmp_path):
    from llm import local_setup_disk as own

    model_dir = tmp_path / "missing" / "models"
    size = 106_318_234_757
    monkeypatch.setattr(own.shutil, "disk_usage", lambda _p: usage(0, 0, int(size * 1.1) - 1))
    with pytest.raises(own.DiskSpaceError, match=r"needs 117 GB of free disk space and this computer has 117 GB"):
        own.require_free_disk(size, model_dir, REPO)
    monkeypatch.setattr(own.shutil, "disk_usage", lambda _p: usage(0, 0, 50 * 1000**3))
    with pytest.raises(own.DiskSpaceError, match=r"Free up about 67 GB"):
        own.require_free_disk(size, model_dir, REPO)
    monkeypatch.setattr(own.shutil, "disk_usage", lambda _p: usage(0, 0, int(size * 1.1)))
    own.require_free_disk(size, model_dir, REPO)


def test_disk_check_reads_the_volume_of_the_reported_directory(monkeypatch, tmp_path):
    from llm import local_setup_disk as own

    read = []
    monkeypatch.setattr(own.shutil, "disk_usage", lambda path: read.append(path) or usage(0, 0, 10**15))
    own.require_free_disk(1000, tmp_path / "not" / "yet" / "made", REPO)
    assert read == [tmp_path]


def test_disk_check_credits_bytes_already_downloaded(monkeypatch, tmp_path):
    from llm import local_setup_disk as own

    folder = tmp_path / REPO
    folder.mkdir(parents=True)
    (folder / "part.safetensors").write_bytes(b"x" * 600)
    # Half-downloaded: 1000 bytes total, 600 present, so 400 (+10% = 440) remain.
    monkeypatch.setattr(own.shutil, "disk_usage", lambda _p: usage(0, 0, 439))
    with pytest.raises(own.DiskSpaceError):
        own.require_free_disk(1000, tmp_path, REPO)
    monkeypatch.setattr(own.shutil, "disk_usage", lambda _p: usage(0, 0, 440))
    own.require_free_disk(1000, tmp_path, REPO)
    # Fully downloaded: passes with almost no free space.
    (folder / "rest.safetensors").write_bytes(b"x" * 400)
    monkeypatch.setattr(own.shutil, "disk_usage", lambda _p: usage(0, 0, 0))
    own.require_free_disk(1000, tmp_path, REPO)
    # Empty / different repo needs the full size.
    monkeypatch.setattr(own.shutil, "disk_usage", lambda _p: usage(0, 0, 1099))
    with pytest.raises(own.DiskSpaceError):
        own.require_free_disk(1000, tmp_path, "org/other")


def test_disk_check_logs_a_warning_and_proceeds_when_space_or_directory_is_unknown(monkeypatch, tmp_path, caplog):
    from llm import local_setup_disk as own

    def boom(_p):
        raise PermissionError("denied")

    monkeypatch.setattr(own.shutil, "disk_usage", boom)
    with caplog.at_level("WARNING"):
        own.require_free_disk(10**12, tmp_path, REPO)
        own.require_free_disk(10**12, None, REPO)
    assert caplog.text.count("Free disk space check skipped") == 2


def test_the_download_checks_the_directory_omlx_reports(monkeypatch, tmp_path):
    order = []

    async def settings(cfg):
        assert cfg == {"server_id": "srv"}
        return {"ok": True, "settings": {"model": {"model_dir": str(tmp_path)}}}

    async def download(server_id, report, chosen):
        order.append(("download", chosen.repository))

    monkeypatch.setattr("llm.providers.omlx_settings.get_settings", settings)
    monkeypatch.setattr(local_setup, "require_free_disk", lambda *args: order.append(("disk", *args)))
    monkeypatch.setattr(local_setup, "_wait_for_download", download)
    asyncio.run(local_setup._download("srv", lambda _stage: None, MODEL))
    assert order == [
        ("disk", MODEL.download_size_bytes, tmp_path, MODEL.repository), ("download", MODEL.repository),
    ]


@pytest.mark.parametrize("reply", [
    {"ok": False, "message": "oMLX is unreachable"}, {"ok": True, "settings": {}},
    {"ok": True, "settings": {"model": {"model_dir": ""}}},
])
def test_an_unreported_model_directory_reaches_the_check_as_unknown(monkeypatch, reply):
    async def settings(_cfg):
        return reply

    monkeypatch.setattr("llm.providers.omlx_settings.get_settings", settings)
    assert asyncio.run(downloads.download_dir("srv")) is None


def _fake_new_omlx(monkeypatch, download, disk=lambda *_args: None, optimal=None):
    """Stub every oMLX call a new-install flight makes; ``download`` is the one under test."""
    wrote = []

    async def apply_optimal(server_id, model):
        wrote.append("optimal")
        return optimal or runtime.OptimalResult("unsupported")

    async def healthy(port):
        return None

    async def register(*, port=None, **kwargs):
        return "srv"

    async def secured(server_id, key):
        return None

    async def global_settings(server_id):
        wrote.append("global")

    async def model_settings(server_id, model):
        wrote.append((model.repository, model.context_window))

    async def discover(server_id):
        return {"ok": True}

    async def registered_key(host, port):
        return "k"

    async def model_dir(server_id):
        return None

    import infra.state.ws
    import llm.catalog_network_models
    from llm.local_model import OMLX_PIN

    monkeypatch.setattr(local_setup, "require_free_disk", disk)
    monkeypatch.setattr(downloads, "download_dir", model_dir)


    async def start_managed(_report):
        return "srv", 5200

    monkeypatch.setattr("llm.local_setup_flow.start_managed_omlx", start_managed)

    monkeypatch.setattr(local_setup, "_wait_for_download", download)
    monkeypatch.setattr(flow, "configure_model_settings", model_settings)
    monkeypatch.setattr(flow, "apply_optimal_settings", apply_optimal)
    monkeypatch.setattr(llm.catalog_network_models, "discover_network_models", discover)
    monkeypatch.setattr(infra.state.ws, "notify_models_changed", lambda: None)
    return wrote


def test_a_full_disk_fails_the_flight_before_the_download_is_requested(monkeypatch):
    requested = []

    async def download(server_id, progress, chosen):
        requested.append(chosen.repository)

    def full(*_args):
        raise RuntimeError("This download needs 117 GB of free disk space and this computer has 1 GB.")

    wrote = _fake_new_omlx(monkeypatch, download, disk=full)

    result = asyncio.run(flow._run_flight(model=BIG))

    assert result["ok"] is False and "117 GB" in result["message"]
    assert requested == [] and wrote == []


def test_a_download_that_outlives_the_follow_names_the_model_and_resumes_it(monkeypatch):
    attempts = []

    async def download(server_id, progress, chosen):
        attempts.append(chosen.repository)
        if len(attempts) == 1:
            raise flow.StillDownloading()

    wrote = _fake_new_omlx(monkeypatch, download)

    first = asyncio.run(flow._run_flight(model=MODEL))

    assert first["downloading"] is True and wrote == []
    assert first["message"] == "Qwen3.8 27B is still downloading in oMLX; CORSIA stopped following it."
    assert first["next_action"] == f"Run `{flow.SETUP_COMMAND}` to follow it again."

    # The same command runs the same setup again: it resumes and writes the model's settings.
    second = asyncio.run(flow._run_flight(model=MODEL))

    assert second["ok"] is True and second["model"] == MODEL.model
    assert attempts == [MODEL.repository] * 2
    assert wrote == ["optimal", (MODEL.repository, MODEL.context_window)]


def test_a_failed_flight_points_at_the_one_setup_command(monkeypatch):
    async def download(server_id, progress, chosen):
        raise RuntimeError("oMLX could not start the model download (HTTP 500).")

    _fake_new_omlx(monkeypatch, download)
    result = asyncio.run(flow._run_flight(model=MODEL))
    assert result["ok"] is False
    assert result["next_action"] == (
        f"Run `{flow.SETUP_COMMAND}` again after fixing this, or add a subscription/API key.")
    assert flow.SETUP_COMMAND == "corsia local setup"


async def _downloaded(server_id, progress, chosen):
    return None


def test_a_new_install_applies_the_benchmark_settings_after_the_starter_download(monkeypatch):
    wrote = _fake_new_omlx(monkeypatch, _downloaded, optimal=runtime.OptimalResult("applied", 41.0))

    result = asyncio.run(flow._run_flight(model=MODEL))

    assert result["ok"] is True
    # The benchmark write carries CORSIA's required fields, so the plain writer is skipped.
    assert wrote == ["optimal"]


def test_a_new_install_without_a_benchmark_writes_corsias_own_model_settings(monkeypatch):
    wrote = _fake_new_omlx(monkeypatch, _downloaded, optimal=runtime.OptimalResult("not_found"))

    assert asyncio.run(flow._run_flight(model=MODEL))["ok"] is True
    assert wrote == ["optimal", (MODEL.repository, MODEL.context_window)]


def test_an_already_connected_local_model_keeps_its_settings(monkeypatch):
    async def download(server_id, progress, chosen):
        raise AssertionError("an existing local model is never downloaded again")

    wrote = _fake_new_omlx(monkeypatch, download)
    monkeypatch.setattr("llm.local_sources.connected_local_server", lambda: {"id": "mine"})

    result = asyncio.run(flow._run_flight(model=MODEL))

    assert result["existing"] is True and result["server_id"] == "mine"
    assert wrote == []


def test_a_persons_own_omlx_never_stands_in_for_corsias_server(monkeypatch):
    from llm import local_sources

    own = {"id": "mine", "provider": "omlx", "kind": "network", "status": "online", "port": 8000}
    legacy = {"id": "old", "provider": "omlx", "kind": "managed", "status": "online", "port": 8200}
    managed = {"id": "corsia-managed", "provider": "omlx", "kind": "managed", "source": "corsia",
               "instance_id": "corsia-managed", "status": "online", "port": 8210}
    servers = [own, legacy]
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: servers)
    monkeypatch.setattr(local_sources, "catalog_membership",
                        lambda: ({"omlx@mine", "omlx@old", "omlx@corsia-managed"}, set()))
    started: list[str] = []

    async def start_managed(_report):
        started.append("corsia-managed")
        servers.append(managed)
        return "corsia-managed", 8210

    assert local_sources.connected_local_server() is None
    resolved = asyncio.run(local_sources.local_server_for_install(lambda _stage: None, start_managed=start_managed))
    assert resolved == ("corsia-managed", 8210, False) and started == ["corsia-managed"]
    assert local_sources.connected_local_server() == managed


def test_settings_download_uses_only_corsias_managed_server(monkeypatch):
    from llm import local_settings_download

    downloaded: list[str] = []

    async def download(server_id, _report, _model):
        downloaded.append(server_id)

    async def start_managed(_report):
        return "corsia-managed", 8210

    monkeypatch.setattr(local_setup, "_download", download)
    monkeypatch.setattr(local_settings_download, "start_managed_omlx", start_managed)

    assert asyncio.run(local_settings_download._managed_download(lambda _stage: None, MODEL)) == "corsia-managed"
    assert downloaded == ["corsia-managed"]


@pytest.fixture
def client(monkeypatch):
    from api.provider_sources import router
    from infra.auth import require_user, require_user_header_only

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: {"id": "u"}
    app.dependency_overrides[require_user_header_only] = lambda: {"id": "u"}
    return TestClient(app)


def test_route_starts_the_flight_with_no_model_choice(client, monkeypatch):
    calls = []

    async def begin(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "in_progress": True}

    monkeypatch.setattr("llm.local_setup_flow.begin_local_setup", begin)

    assert client.post("/api/providers/local/setup").json() == {"ok": True, "in_progress": True}
    assert calls[-1] == {"no_subscription_only": False, "automatic": False, "onboarding": False}


def test_route_returns_backend_recovery_when_starting_fails(client, monkeypatch):
    from llm.local_setup_results import local_setup_error

    async def begin(**_kwargs):
        return local_setup_error("could not start")

    monkeypatch.setattr("llm.local_setup_flow.begin_local_setup", begin)

    response = client.post("/api/providers/local/setup")

    assert response.status_code == 200
    assert response.json()["fix"] == "CORSIA couldn’t set up the local model."
