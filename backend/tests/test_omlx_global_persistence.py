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
