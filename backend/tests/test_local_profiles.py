"""Local model profiles: one list, one window rule, validation, route and flight wiring.

Everything is mocked: no model is loaded or downloaded and no disk is read.
"""

from __future__ import annotations

import asyncio
import json
from collections import namedtuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm import local_profiles, local_setup, local_setup_gates as gates, local_setup_operation

GIB = 1024**3
PROFILE_IDS = ["starter", "qwen-27b-oq4e", "qwen-27b-oq8e", "flash-next-oq4e", "flash-next-oq6e"]


@pytest.fixture(autouse=True)
def fresh_flight(monkeypatch):
    local_setup_operation.clear()
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr(local_setup, "_emit", lambda action, **properties: None)
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: False)


@pytest.mark.parametrize("ram_gib, expected", [
    (8, "starter"), (47.9, "starter"), (48, "qwen-27b-oq4e"), (63.9, "qwen-27b-oq4e"),
    (64, "qwen-27b-oq8e"), (127.9, "qwen-27b-oq8e"), (128, "flash-next-oq4e"),
    (191.9, "flash-next-oq4e"), (192, "flash-next-oq6e"), (512, "flash-next-oq6e"),
])
def test_profile_for_ram_follows_the_band_table_at_its_boundaries(ram_gib, expected):
    assert gates.profile_for_ram(int(ram_gib * GIB)).id == expected


# The one rule's computed window for each profile, pinned so a change to the rule,
# a profile's sizes or its KV cost shows here. Each larger profile is eligible at its
# band's threshold and not below it; the default stays eligible from 24 GiB, as before.
@pytest.mark.parametrize("profile_id, ram_gib, window, eligible", [
    ("starter", 16, 8192, False), ("starter", 24, 49152, True), ("starter", 32, 81920, True),
    ("starter", 48, 155648, True), ("starter", 64, 221184, True),
    ("qwen-27b-oq4e", 48, 114688, True), ("qwen-27b-oq4e", 47.9, 114688, False),
    ("qwen-27b-oq8e", 64, 114688, True), ("qwen-27b-oq8e", 48, 49152, False),
    ("flash-next-oq4e", 128, 65536, True), ("flash-next-oq4e", 96, 0, False),
    ("flash-next-oq6e", 192, 262144, True), ("flash-next-oq6e", 128, 0, False),
])
def test_the_one_rule_pins_every_profiles_window_and_verdict(profile_id, ram_gib, window, eligible):
    profile = local_profiles.profile_by_id(profile_id)
    result = gates.profile_eligibility(profile, int(ram_gib * GIB))
    assert result.context_window == window and result.eligible is eligible
    assert window % 8192 == 0


def test_the_window_follows_the_stated_formula():
    profile = local_profiles.profile_by_id("qwen-27b-oq4e")
    ram = 48 * GIB
    hold_back = min(8, max(3, 0.08 * 48)) * GIB
    spare = ram - hold_back - 0.125 * ram - profile.resident_weight_bytes
    expected = int(spare / (profile.kv_mb_per_token * 1e6) // 8192) * 8192
    assert gates.context_window(profile, ram) == expected
    # Too small a Mac gets nothing, and the window never passes 256K.
    assert gates.context_window(profile, 24 * GIB) < profile.min_context
    assert gates.context_window(profile, 4096 * GIB) == 262_144


def test_profiles_are_one_ordered_list_with_the_default_first():
    assert [p.id for p in local_profiles.PROFILES] == PROFILE_IDS
    assert local_profiles.DEFAULT_PROFILE.id == "starter"
    assert local_profiles.DEFAULT_PROFILE.repository == "prism-ml/Ternary-Bonsai-2-27B-mlx-2bit"
    for profile in local_profiles.PROFILES:
        assert len(profile.sized_at_revision) == 40 and profile.repository.count("/") == 1
        assert profile.model == profile.repository.split("/")[1]
        assert profile.model_settings["chat_template_kwargs"] == {"reasoning_effort": "medium"}
    assert all(p.model_settings["mtp_enabled"] is True for p in local_profiles.PROFILES[1:])
    assert "mtp_enabled" not in local_profiles.DEFAULT_PROFILE.model_settings


def test_omlx_global_settings_are_stored_once():
    raw = json.loads(local_profiles._PATH.read_text())
    assert all("omlx_global_settings" not in profile for profile in raw["profiles"])
    assert local_profiles.OMLX_GLOBAL_SETTINGS == raw["omlx_global_settings"]


def test_loader_rejects_malformed_profile_files(tmp_path):
    good = json.loads(local_profiles._PATH.read_text())
    first, second = good["profiles"][0], good["profiles"][1]

    def load(payload):
        path = tmp_path / "profiles.json"
        path.write_text(json.dumps(payload))
        return local_profiles.load_profiles(path)

    assert len(load(good)[1]) == 5
    for bad in (
        [],
        {**good, "extra": 1},
        {**good, "profiles": []},
        {**good, "profiles": [{**first, "extra": 1}]},
        {**good, "profiles": [{k: v for k, v in first.items() if k != "sized_at_revision"}]},
        {**good, "profiles": [first, {**second, "min_ram_gib": "48"}]},
        {**good, "profiles": [first, first]},
        {**good, "profiles": [second, first]},
        {**good, "profiles": [second]},
    ):
        with pytest.raises(RuntimeError):
            load(bad)
    with pytest.raises(RuntimeError, match="unreadable"):
        local_profiles.load_profiles(tmp_path / "absent.json")


def test_recommended_profile_names_the_model_size_and_minimum_ram():
    # The default's band is offered too: the offer runs when install skipped local setup.
    assert gates.recommended_profile(32 * GIB, False)["id"] == "starter"
    assert gates.recommended_profile(16 * GIB, False) is None
    assert gates.recommended_profile(64 * GIB, True) is None
    assert gates.recommended_profile(64 * GIB, False) == {
        "id": "qwen-27b-oq8e", "display_name": "Qwen3.8 27B oQ8e",
        "download_size_bytes": 30_024_803_622, "download_size_gb": 30.0, "min_ram_gib": 64,
    }


def test_resolve_profile_refuses_unknown_too_big_and_foreign_requests():
    with pytest.raises(gates.ProfileRefused, match="Unknown"):
        gates.resolve_profile("nope", 256 * GIB, False)
    with pytest.raises(gates.ProfileRefused, match="at least 192 GB"):
        gates.resolve_profile("flash-next-oq6e", 128 * GIB, False)
    with pytest.raises(gates.ProfileRefused, match="does not add models to an oMLX CORSIA did not install"):
        gates.resolve_profile("qwen-27b-oq4e", 48 * GIB, True)
    assert gates.resolve_profile("qwen-27b-oq4e", 48 * GIB, False).id == "qwen-27b-oq4e"
    assert gates.resolve_profile(None, 8 * GIB, True) is local_profiles.DEFAULT_PROFILE
    assert gates.resolve_profile("starter", 8 * GIB, True) is local_profiles.DEFAULT_PROFILE


def _ram(monkeypatch, gib):
    monkeypatch.setattr("infra.host_resources.host_total_ram_bytes", lambda: gib * GIB)


def _record_flight(monkeypatch, seen: list):
    async def flight(eligibility, *, profile, **kwargs):
        seen.append((profile.id, eligibility.eligible, eligibility.context_window))
        return {"ok": True}

    monkeypatch.setattr(local_setup, "_run_flight", flight)


async def _begin(**kwargs):
    local_setup._flight.task = None
    result = await local_setup.begin_local_setup(**kwargs)
    if local_setup._flight.task is not None:
        await local_setup._flight.task
    return result


def test_begin_with_a_profile_runs_the_flight_with_that_profile(monkeypatch):
    _ram(monkeypatch, 64)
    seen = []
    _record_flight(monkeypatch, seen)
    result = asyncio.run(_begin(profile_id="qwen-27b-oq8e"))
    assert result["in_progress"] is True
    assert seen == [("qwen-27b-oq8e", True, 114688)]


def test_omitted_and_explicit_default_take_one_path(monkeypatch):
    _ram(monkeypatch, 24)
    seen = []
    _record_flight(monkeypatch, seen)
    asyncio.run(_begin())
    asyncio.run(_begin(profile_id="starter"))
    assert seen == [("starter", True, 49152)] * 2


def test_begin_refuses_a_bad_profile_before_changing_flight_state(monkeypatch):
    _ram(monkeypatch, 64)
    local_setup._flight.result = {"ok": True, "previous": True}
    for bad in ("flash-next-oq6e", "nope"):
        with pytest.raises(gates.ProfileRefused):
            asyncio.run(local_setup.begin_local_setup(profile_id=bad))
    assert local_setup._flight.result == {"ok": True, "previous": True}


def test_a_larger_profile_is_refused_up_front_for_an_omlx_the_person_installed(monkeypatch):
    _ram(monkeypatch, 64)
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: True)
    local_setup._flight.result = {"ok": True, "previous": True}
    with pytest.raises(gates.ProfileRefused, match="does not add models to an oMLX CORSIA did not install"):
        asyncio.run(local_setup.begin_local_setup(profile_id="qwen-27b-oq8e"))
    assert local_setup._flight.result == {"ok": True, "previous": True} and local_setup._flight.task is None
    # The default keeps adopting that oMLX.
    seen = []
    _record_flight(monkeypatch, seen)
    asyncio.run(_begin())
    assert [profile for profile, *_ in seen] == ["starter"]


def test_download_and_settings_use_the_chosen_profile(monkeypatch):
    profile = local_profiles.profile_by_id("flash-next-oq4e")
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"success": True, "task": {"task_id": "t1"}, "tasks": [{"task_id": "t1", "status": "completed"}]}

    async def admin_request(_cfg, method, path, *, json=None, timeout=None):
        calls.append((method, path, json))
        return Response()

    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)
    asyncio.run(local_setup._wait_for_download("srv", lambda _m: None, profile))
    asyncio.run(local_setup._configure_model_settings("srv", 262144, profile))

    assert calls[0] == ("POST", "/hf/download", {"repo_id": profile.repository, "hf_token": ""})
    method, path, payload = calls[-1]
    assert (method, path) == ("PUT", f"/models/{profile.model}/settings")
    assert payload == {**profile.model_settings, "max_context_window": 262144}


REPO = "org/model"
usage = namedtuple("usage", "total used free")


def test_disk_check_fails_with_the_space_needed_and_passes_with_room(monkeypatch, tmp_path):
    from llm import local_setup_ownership as own

    model_dir = tmp_path / "missing" / "models"
    size = 106_318_234_757
    monkeypatch.setattr(own.shutil, "disk_usage", lambda _p: usage(0, 0, int(size * 1.1) - 1))
    with pytest.raises(own.DiskSpaceError, match=r"needs 117 GB of free disk space and this Mac has 117 GB"):
        own.require_free_disk(size, model_dir, REPO)
    monkeypatch.setattr(own.shutil, "disk_usage", lambda _p: usage(0, 0, 50 * 1000**3))
    with pytest.raises(own.DiskSpaceError, match=r"Free up about 67 GB"):
        own.require_free_disk(size, model_dir, REPO)
    monkeypatch.setattr(own.shutil, "disk_usage", lambda _p: usage(0, 0, int(size * 1.1)))
    own.require_free_disk(size, model_dir, REPO)


def test_disk_check_reads_the_volume_of_the_reported_directory(monkeypatch, tmp_path):
    from llm import local_setup_ownership as own

    read = []
    monkeypatch.setattr(own.shutil, "disk_usage", lambda path: read.append(path) or usage(0, 0, 10**15))
    own.require_free_disk(1000, tmp_path / "not" / "yet" / "made", REPO)
    assert read == [tmp_path]


def test_disk_check_credits_bytes_already_downloaded(monkeypatch, tmp_path):
    from llm import local_setup_ownership as own

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
    from llm import local_setup_ownership as own

    def boom(_p):
        raise PermissionError("denied")

    monkeypatch.setattr(own.shutil, "disk_usage", boom)
    with caplog.at_level("WARNING"):
        own.require_free_disk(10**12, tmp_path, REPO)
        own.require_free_disk(10**12, None, REPO)
    assert caplog.text.count("Free disk space check skipped") == 2


def test_the_download_checks_the_directory_omlx_reports(monkeypatch, tmp_path):
    profile = local_profiles.profile_by_id("qwen-27b-oq4e")
    order = []

    async def settings(cfg):
        assert cfg == {"server_id": "srv"}
        return {"ok": True, "settings": {"model": {"model_dir": str(tmp_path)}}}

    async def download(server_id, report, chosen):
        order.append(("download", chosen.id))

    monkeypatch.setattr("llm.providers.omlx_settings.get_settings", settings)
    monkeypatch.setattr(local_setup, "require_free_disk", lambda *args: order.append(("disk", *args)))
    monkeypatch.setattr(local_setup, "_wait_for_download", download)
    asyncio.run(local_setup._download("srv", lambda _stage: None, profile))
    assert order == [
        ("disk", profile.download_size_bytes, tmp_path, profile.repository), ("download", profile.id),
    ]


@pytest.mark.parametrize("reply", [
    {"ok": False, "message": "oMLX is unreachable"}, {"ok": True, "settings": {}},
    {"ok": True, "settings": {"model": {"model_dir": ""}}},
])
def test_an_unreported_model_directory_reaches_the_check_as_unknown(monkeypatch, reply):
    async def settings(_cfg):
        return reply

    monkeypatch.setattr("llm.providers.omlx_settings.get_settings", settings)
    assert asyncio.run(local_setup._download_dir("srv")) is None


def _fake_new_omlx(monkeypatch, download, disk=lambda *_args: None):
    """Stub every oMLX call a new-install flight makes; ``download`` is the one under test."""
    wrote = []

    async def healthy(port):
        return None

    async def register(*, port=None, **kwargs):
        return "srv"

    async def secured(server_id, key):
        return None

    async def global_settings(server_id):
        wrote.append("global")

    async def model_settings(server_id, window, profile):
        wrote.append((profile.id, window))

    async def discover(server_id):
        return {"ok": True}

    async def registered_key(host, port):
        return "k"

    async def model_dir(server_id):
        return None

    import infra.state.ws
    import llm.catalog_network_models

    monkeypatch.setattr(local_setup, "require_free_disk", disk)
    monkeypatch.setattr(local_setup, "_download_dir", model_dir)
    monkeypatch.setattr(local_setup, "_omlx_supports_loopback_auth", lambda: True)
    monkeypatch.setattr(local_setup, "_ensure_omlx", lambda *a, **k: None)
    monkeypatch.setattr(local_setup, "_run", lambda command: None)
    monkeypatch.setattr(local_setup, "own_omlx_port", lambda: 5200)
    monkeypatch.setattr(local_setup, "own_omlx_admin_key", lambda: "k")
    monkeypatch.setattr(local_setup, "registered_local_key", registered_key)
    monkeypatch.setattr(local_setup, "_wait_for_health", healthy)
    monkeypatch.setattr(local_setup, "_register_loopback_server", register)
    monkeypatch.setattr(local_setup, "secure_local_omlx", secured)
    monkeypatch.setattr(local_setup, "_configure_global", global_settings)
    monkeypatch.setattr(local_setup, "_wait_for_download", download)
    monkeypatch.setattr(local_setup, "_configure_model_settings", model_settings)
    monkeypatch.setattr(llm.catalog_network_models, "discover_network_models", discover)
    monkeypatch.setattr(infra.state.ws, "notify_models_changed", lambda: None)
    return wrote


def test_a_full_disk_fails_the_flight_before_the_download_is_requested(monkeypatch):
    requested = []

    async def download(server_id, progress, chosen):
        requested.append(chosen.id)

    def full(*_args):
        raise RuntimeError("This download needs 117 GB of free disk space and this Mac has 1 GB.")

    wrote = _fake_new_omlx(monkeypatch, download, disk=full)
    profile = local_profiles.profile_by_id("flash-next-oq4e")

    result = asyncio.run(local_setup._run_flight(local_setup.Eligibility(262144, True), profile=profile))

    assert result["ok"] is False and "117 GB" in result["message"]
    assert requested == [] and wrote == ["global"]


def test_a_download_that_outlives_the_follow_names_the_profile_and_resumes_by_profile(monkeypatch):
    profile = local_profiles.profile_by_id("qwen-27b-oq8e")
    attempts = []

    async def download(server_id, progress, chosen):
        attempts.append(chosen.id)
        if len(attempts) == 1:
            raise local_setup.StillDownloading()

    wrote = _fake_new_omlx(monkeypatch, download)
    eligibility = local_setup.Eligibility(114688, True)

    first = asyncio.run(local_setup._run_flight(eligibility, profile=profile))

    assert first["downloading"] is True and wrote == ["global"]
    assert first["message"] == f"{profile.display_name} is still downloading in oMLX; CORSIA stopped following it."
    assert first["next_action"] == "Run `corsia local setup --profile qwen-27b-oq8e` to follow it again."

    # The same command runs the same setup again: it resumes and writes the profile's model settings.
    second = asyncio.run(local_setup._run_flight(eligibility, profile=profile))

    assert second["ok"] is True and second["model"] == profile.model
    assert attempts == ["qwen-27b-oq8e"] * 2 and wrote == ["global", "global", ("qwen-27b-oq8e", 114688)]


@pytest.mark.parametrize("profile_id", ["qwen-27b-oq8e", "starter"])
def test_a_failed_flight_points_at_its_own_profile(monkeypatch, profile_id):
    async def download(server_id, progress, chosen):
        raise RuntimeError("oMLX could not start the model download (HTTP 500).")

    _fake_new_omlx(monkeypatch, download)
    profile = local_profiles.profile_by_id(profile_id)
    result = asyncio.run(local_setup._run_flight(local_setup.Eligibility(114688, True), profile=profile))
    assert result["ok"] is False
    assert result["next_action"] == (
        f"Run `{local_profiles.setup_command(profile_id)}` again after fixing this, or add a subscription/API key.")
    # The default needs no flag, matching what the CLI prints for it.
    assert ("--profile" in result["next_action"]) == (profile_id != "starter")


@pytest.fixture
def client(monkeypatch):
    from api.provider_sources import router
    from infra.auth import require_user, require_user_header_only

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: {"id": "u"}
    app.dependency_overrides[require_user_header_only] = lambda: {"id": "u"}
    return TestClient(app)


def test_route_passes_the_profile_and_turns_a_refusal_into_a_400(client, monkeypatch):
    calls = []

    async def begin(**kwargs):
        calls.append(kwargs)
        if kwargs["profile_id"] == "bad":
            raise gates.ProfileRefused("Unknown local model profile 'bad'.")
        return {"ok": True, "in_progress": True}

    monkeypatch.setattr("llm.local_setup.begin_local_setup", begin)
    monkeypatch.setattr("llm.local_setup_ownership.existing_omlx_requires_consent", lambda: False)

    assert client.post("/api/providers/local/setup").json() == {"ok": True, "in_progress": True}
    assert calls[-1] == {"no_subscription_only": False, "automatic": False, "profile_id": None}
    assert client.post("/api/providers/local/setup", json={"profile": "qwen-27b-oq4e"}).status_code == 200
    assert calls[-1]["profile_id"] == "qwen-27b-oq4e"
    refused = client.post("/api/providers/local/setup", json={"profile": "bad"})
    assert refused.status_code == 400 and "Unknown" in refused.json()["detail"]


def test_status_route_says_whose_omlx_it_is_and_recommends_only_for_corsias(client, monkeypatch):
    foreign = {"value": False}
    monkeypatch.setattr("llm.local_setup_ownership.existing_install_is_foreign", lambda: foreign["value"])
    _ram(monkeypatch, 128)
    body = client.get("/api/providers/local/setup").json()
    assert body["state"] == "idle" and body["omlx_is_persons_own"] is False
    assert body["recommended_profile"]["id"] == "flash-next-oq4e"
    foreign["value"] = True
    body = client.get("/api/providers/local/setup").json()
    assert body["omlx_is_persons_own"] is True and body["recommended_profile"] is None
    foreign["value"] = False
    _ram(monkeypatch, 16)
    assert client.get("/api/providers/local/setup").json()["recommended_profile"] is None
