"""Managed launch and setup's settings write preserve oMLX-owned global settings."""

import asyncio
import json

from llm import local_setup_runtime
from llm.providers import omlx_supervisor_config


def _save(tmp_path, scheduler: dict) -> None:
    settings_path = tmp_path / "managed-omlx" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"scheduler": scheduler}))


def test_managed_launch_keeps_saved_scheduler_values(tmp_path, monkeypatch):
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    _save(tmp_path, {"max_concurrent_requests": 6, "prefill_priority": "speed"})
    result = omlx_supervisor_config.managed_launch_settings({})
    assert result["max_concurrent_requests"] == 6
    assert result["prefill_priority"] == "speed"


def test_managed_launch_uses_shipped_defaults_for_missing_settings(tmp_path, monkeypatch):
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    result = omlx_supervisor_config.managed_launch_settings({})
    assert result["max_concurrent_requests"] == 2
    assert result["prefill_priority"] == "context"


def test_setup_settings_write_keeps_saved_values_and_fills_the_rest(tmp_path, monkeypatch):
    # Setup, a Settings download and a missing-server start all end in configure_global.
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    _save(tmp_path, {"max_concurrent_requests": 6})
    posted: list[dict] = []

    async def save_global_settings(_cfg, pairs):
        posted.append(pairs)
        return {"ok": True}

    monkeypatch.setattr("llm.providers.omlx_settings.save_global_settings", save_global_settings)
    asyncio.run(local_setup_runtime.configure_global("server"))

    assert posted == [{"max_concurrent_requests": 6, "prefill_priority": "context",
                       "skip_api_key_verification": True}]


def _save_settings(tmp_path, body: dict) -> None:
    settings_path = tmp_path / "managed-omlx" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(body))


def test_managed_launch_keeps_the_cache_folder_the_person_chose(tmp_path, monkeypatch):
    # oMLX saves launch folders back into settings.json, so passing any other folder would overwrite it.
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    chosen = str(tmp_path / "another-drive" / "kv")
    _save_settings(tmp_path, {"base_path": str(tmp_path / "managed-omlx"),
                              "cache": {"enabled": True, "ssd_cache_dir": chosen}})
    assert omlx_supervisor_config.managed_launch_settings({})["paged_ssd_cache_dir"] == chosen


def test_managed_launch_uses_the_default_cache_folder_when_none_is_chosen(tmp_path, monkeypatch):
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    expected = str(tmp_path / "managed-omlx" / "cache")
    assert omlx_supervisor_config.managed_launch_settings({})["paged_ssd_cache_dir"] == expected
    _save_settings(tmp_path, {"base_path": str(tmp_path / "managed-omlx"),
                              "cache": {"enabled": True, "ssd_cache_dir": expected}})
    assert omlx_supervisor_config.managed_launch_settings({})["paged_ssd_cache_dir"] == expected


def test_a_default_cache_folder_saved_under_an_older_base_follows_the_current_one(tmp_path, monkeypatch):
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    old_base = tmp_path / "older-root" / "managed-omlx"
    _save_settings(tmp_path, {"base_path": str(old_base), "cache": {"ssd_cache_dir": str(old_base / "cache")}})
    result = omlx_supervisor_config.managed_launch_settings({})
    assert result["paged_ssd_cache_dir"] == str(tmp_path / "managed-omlx" / "cache")


def test_a_cache_turned_off_stays_off(tmp_path, monkeypatch):
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    _save_settings(tmp_path, {"cache": {"enabled": False, "ssd_cache_dir": str(tmp_path / "kv")}})
    assert omlx_supervisor_config.managed_launch_settings({})["paged_ssd_cache_dir"] is None
    _save_settings(tmp_path, {"cache": {"ssd_cache_dir": str(tmp_path / "kv")}})
    assert omlx_supervisor_config.managed_launch_settings({"no_cache": True})["paged_ssd_cache_dir"] is None
