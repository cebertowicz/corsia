"""The own-oMLX settings difference and the flight kind on the local source row."""

from __future__ import annotations

import asyncio

import pytest

from llm import local_setup, local_sources, omlx_global_settings, own_omlx_management
from llm.local_model import LocalModel
from llm.local_profiles import OMLX_GLOBAL_SETTINGS

SERVER = {"id": "local-server"}


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    monkeypatch.setattr(omlx_global_settings, "_observed", {})
    monkeypatch.setattr(omlx_global_settings, "_observed_model", {})
    monkeypatch.setattr(own_omlx_management, "record_path", lambda: tmp_path / "own-omlx-management.json")
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kw: [
        {"id": "local-server", "status": "online", "kind": "local"}])
    monkeypatch.setattr("llm.local_setup_ownership.existing_install_is_foreign", lambda: True)


def _observe(monkeypatch, result):
    async def reading(_cfg):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(omlx_global_settings, "get_settings", reading)
    asyncio.run(omlx_global_settings.record_observation(SERVER))


def _settings(row):
    return row["omlx_settings"]


def test_a_differing_setting_is_named_with_current_and_recommended(monkeypatch):
    _observe(monkeypatch, {"ok": True, "settings": {"max_concurrent_requests": 8, "prefill_priority": "context"}})

    settings = _settings(local_sources.omlx_source())
    assert settings["differences"] == [{"setting": "max_concurrent_requests", "current": 8,
                                         "recommended": OMLX_GLOBAL_SETTINGS["max_concurrent_requests"]}]
    assert isinstance(settings["fingerprint"], str)


def test_settings_nested_in_groups_are_found(monkeypatch):
    _observe(monkeypatch, {"ok": True, "settings": {"scheduler": {"max_concurrent_requests": 8,
                                                                   "prefill_priority": "decode"}}})

    assert _settings(local_sources.omlx_source())["differences"] == [
        {"setting": "max_concurrent_requests", "current": 8,
         "recommended": OMLX_GLOBAL_SETTINGS["max_concurrent_requests"]}]


def test_matching_settings_give_an_empty_list(monkeypatch):
    _observe(monkeypatch, {"ok": True, "settings": dict(OMLX_GLOBAL_SETTINGS)})

    assert _settings(local_sources.omlx_source()) == []


def test_a_setting_oMLX_does_not_report_is_not_a_difference(monkeypatch):
    _observe(monkeypatch, {"ok": True, "settings": {"unrelated": 1}})

    assert _settings(local_sources.omlx_source()) == []


@pytest.mark.parametrize("failure", [
    {"ok": False, "message": "HTTP 500"}, {"ok": True, "settings": {}}, RuntimeError("boom"),
])
def test_a_failed_or_empty_read_is_unknown_never_an_error(monkeypatch, failure):
    _observe(monkeypatch, {"ok": True, "settings": {"max_concurrent_requests": 8}})
    _observe(monkeypatch, failure)

    assert _settings(local_sources.omlx_source()) is None


def test_a_slow_read_is_bounded(monkeypatch):
    async def never(_cfg):
        await asyncio.sleep(60)

    monkeypatch.setattr(omlx_global_settings, "get_settings", never)
    monkeypatch.setattr(omlx_global_settings, "READ_TIMEOUT_SECONDS", 0.01)
    asyncio.run(omlx_global_settings.record_observation(SERVER))

    assert _settings(local_sources.omlx_source()) is None


def test_failed_rearm_retries_when_the_managed_model_is_seen_again(monkeypatch):
    model = LocalModel("org/starter", 1, 32_768)
    attempts = []

    async def model_settings(_cfg, _model):
        return {"ok": True, "settings": {"max_tokens": 1}, "thinking_default": True}

    async def rearm():
        attempts.append(True)
        return len(attempts) == 2

    monkeypatch.setattr(omlx_global_settings, "_managed_model", lambda: model)
    monkeypatch.setattr(omlx_global_settings, "get_model_settings", model_settings)
    monkeypatch.setattr("onboarding.omlx_settings_wake.rearm", rearm)
    _observe(monkeypatch, {"ok": True, "settings": {"max_concurrent_requests": 8}})
    _observe(monkeypatch, {"ok": True, "settings": {"max_concurrent_requests": 8}})

    assert attempts == [True, True]
    assert omlx_global_settings._observed_model[SERVER["id"]][0] == model.model


def test_corsias_own_omlx_never_reports_settings(monkeypatch):
    _observe(monkeypatch, {"ok": True, "settings": {"max_concurrent_requests": 8}})
    monkeypatch.setattr("llm.local_setup_ownership.existing_install_is_foreign", lambda: False)

    assert _settings(local_sources.omlx_source()) is None


def test_reading_sources_never_calls_omlx(monkeypatch):
    _observe(monkeypatch, {"ok": True, "settings": {"max_concurrent_requests": 8}})

    async def forbidden(_cfg):
        raise AssertionError("sources must not call oMLX")

    monkeypatch.setattr(omlx_global_settings, "get_settings", forbidden)
    assert _settings(local_sources.omlx_source())


@pytest.mark.parametrize("kind", ["starter", "setup"])
def test_a_failed_flight_reports_which_kind_it_was(kind):
    local_setup._flight.kind = kind
    local_setup._flight.result = {"ok": False, "message": "disk full"}

    assert local_sources.omlx_source()["setup_kind"] == kind


@pytest.mark.parametrize("kind", ["starter", "setup"])
def test_a_download_that_outlived_following_reports_its_kind(kind):
    local_setup._flight.kind = kind
    local_setup._flight.result = {"ok": True, "downloading": True, "message": "still downloading"}

    assert local_sources.omlx_source()["setup_kind"] == kind


def test_an_idle_source_has_no_kind():
    assert local_sources.omlx_source()["setup_kind"] is None


def test_a_finished_flight_has_no_kind():
    local_setup._flight.kind = "starter"
    local_setup._flight.result = {"ok": True}

    assert local_sources.omlx_source()["setup_kind"] is None


def test_sources_project_diff_and_fingerprint_together(monkeypatch):
    _observe(monkeypatch, {"ok": True, "settings": {"max_concurrent_requests": 8}})

    settings = _settings(local_sources.omlx_source())
    assert settings["differences"][0]["setting"] == "max_concurrent_requests"
    assert settings["fingerprint"] == omlx_global_settings.fingerprint(
        SERVER["id"], settings["differences"])


def test_disallowed_profile_key_is_never_projected(monkeypatch):
    monkeypatch.setattr(omlx_global_settings, "OMLX_GLOBAL_SETTINGS",
                        {"max_concurrent_requests": 1, "host": "0.0.0.0"})
    _observe(monkeypatch, {"ok": True, "settings": {"max_concurrent_requests": 8, "host": "127.0.0.1"}})

    assert [row["setting"] for row in _settings(local_sources.omlx_source())["differences"]] == [
        "max_concurrent_requests"]
