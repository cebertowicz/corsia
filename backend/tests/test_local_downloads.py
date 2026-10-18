from __future__ import annotations

from llm import local_downloads, local_setup, local_setup_ownership, local_sources
from llm.local_model import LocalModel


def test_settings_downloads_are_separate_from_setup_marker(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768, "Qwen 3.8 27B")

    local_downloads.write(local_downloads.Download(model, speed=52.0, server_id="local-server"))

    stored = local_downloads.get(model.model)
    assert stored is not None
    assert stored.model == model
    assert stored.speed == 52.0
    assert stored.view() == {
        "name": "Qwen 3.8 27B", "repository": model.repository, "model": model.model,
        "download_size_bytes": 1_000_000, "download_size_gb": 0.0,
        "context_window": 32_768, "status": "available", "speed": 52.0,
        "detail": None, "server_id": "local-server",
    }
    local_downloads.write(local_downloads.Download(model, status="failed", detail="network unavailable"))
    failed = local_downloads.get(model.model)
    assert failed is not None and failed.status == "failed" and failed.detail == "network unavailable"
    local_downloads.forget(model.model)
    assert local_downloads.all() == []


def test_unreadable_record_is_kept_aside_and_not_overwritten(monkeypatch, tmp_path) -> None:
    target = tmp_path / "local-downloads.json"
    monkeypatch.setattr(local_downloads, "_path", lambda: target)
    target.write_text("{not json", encoding="utf-8")
    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768, "Qwen 3.8 27B")

    assert local_downloads.all() == []
    local_downloads.write(local_downloads.Download(model, server_id="local-server"))

    assert (tmp_path / "local-downloads.json.corrupt").read_text(encoding="utf-8") == "{not json"
    assert [entry.model for entry in local_downloads.all()] == [model]


def test_failed_settings_download_projects_a_durable_retry_target(monkeypatch, tmp_path) -> None:
    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768, "Qwen 3.8 27B")
    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    local_downloads.write(local_downloads.Download(model, status="failed", detail="network unavailable"))
    monkeypatch.setattr(local_sources, "_omlx_state", lambda _membership: {"state": "ready"})
    monkeypatch.setattr(local_setup, "setup_kind", lambda: "settings")
    monkeypatch.setattr(local_setup, "setup_stage", lambda: None)
    monkeypatch.setattr(local_setup_ownership, "existing_install_is_foreign", lambda: False)
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [])

    source = local_sources.omlx_source((set(), set()))

    assert source["settings_download"] == {
        "name": "Qwen 3.8 27B", "repository": model.repository, "percent": None, "failed": True,
    }
