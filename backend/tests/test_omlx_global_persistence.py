"""Managed launch preserves oMLX-owned global settings."""

import json

from llm.providers import omlx_supervisor_config


def test_managed_launch_keeps_saved_scheduler_values(tmp_path, monkeypatch):
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    settings_path = tmp_path / "managed-omlx" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"scheduler": {"max_concurrent_requests": 6, "prefill_priority": "speed"}}))
    result = omlx_supervisor_config.managed_launch_settings({})
    assert result["max_concurrent_requests"] == 6
    assert result["prefill_priority"] == "speed"


def test_managed_launch_uses_shipped_defaults_for_missing_settings(tmp_path, monkeypatch):
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    result = omlx_supervisor_config.managed_launch_settings({})
    assert result["max_concurrent_requests"] == 2
    assert result["prefill_priority"] == "context"
