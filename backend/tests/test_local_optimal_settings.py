from __future__ import annotations

import pytest

from llm import own_omlx_management
from llm.local_model import LocalModel
from llm import local_setup_auth, local_setup_runtime


@pytest.mark.asyncio
async def test_use_mine_returns_without_touching_omlx(monkeypatch) -> None:
    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768)
    monkeypatch.setattr(own_omlx_management, "uses_mine", lambda: True)

    async def unexpected_writer(*_args, **_kwargs):
        raise AssertionError("Use mine must not make an oMLX request")

    from llm.providers import omlx_endpoints
    monkeypatch.setattr(omlx_endpoints, "admin_request", unexpected_writer)
    result = await local_setup_runtime.apply_optimal_settings("server", model)

    assert result.status == "no_omlx"


@pytest.mark.asyncio
async def test_old_omlx_returns_unsupported_without_a_writer(monkeypatch) -> None:
    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768)
    monkeypatch.setattr(own_omlx_management, "uses_mine", lambda: False)
    monkeypatch.setattr(local_setup_auth, "omlx_version", lambda: "0.7.0")

    result = await local_setup_runtime.apply_optimal_settings("server", model)

    assert result.status == "unsupported"


class _Response:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> dict:
        return self._body


@pytest.mark.asyncio
async def test_applied_optimal_settings_use_one_filtered_writer(monkeypatch) -> None:
    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768)
    writes: list[dict] = []
    monkeypatch.setattr(own_omlx_management, "uses_mine", lambda: False)
    monkeypatch.setattr(own_omlx_management, "active", lambda: False)
    monkeypatch.setattr(local_setup_auth, "omlx_version", lambda: "0.7.1rc1")
    monkeypatch.setattr(local_setup_runtime, "thinking_settings", lambda *_args: _empty_settings())

    async def admin_request(_cfg, method, path, **kwargs):
        if method == "PUT":
            writes.append(kwargs["json"])
            return _Response(200, {})
        if path.endswith("/optimal"):
            return _Response(200, {"by_tg": [{"benchmark_id": "benchmark", "tg_tps": 52.0}]})
        if path == "/profile-fields":
            return _Response(200, {"model_specific": ["turboquant_kv_enabled", "turboquant_kv_bits", "dflash_draft_model"]})
        if path == "/models":
            return _Response(200, {"models": [{"id": model.model}]})
        return _Response(200, {})

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def get(self, _url):
            return _Response(200, {"model_settings": {"turboquant_kv_enabled": True, "turboquant_kv_bits": 4, "dflash_draft_model": "missing", "temperature": 0.7}})

    from llm.providers import omlx_endpoints
    monkeypatch.setattr(omlx_endpoints, "admin_request", admin_request)
    monkeypatch.setattr(local_setup_runtime.httpx, "AsyncClient", lambda **_kwargs: Client())

    result = await local_setup_runtime.apply_optimal_settings("server", model)

    assert result.status == "applied"
    assert result.speed == 52.0
    assert len(writes) == 1
    assert writes[0]["turboquant_kv_enabled"] is True
    assert writes[0]["turboquant_kv_bits"] == 4
    assert "dflash_draft_model" not in writes[0]
    assert "temperature" not in writes[0]


@pytest.mark.asyncio
async def test_settings_download_does_not_retry_after_an_optimal_writer_failure(monkeypatch) -> None:
    from llm import local_settings_download

    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768)

    async def managed_download(*_args, **_kwargs):
        return "server", True

    async def failed_optimal(*_args, **_kwargs):
        return local_setup_runtime.OptimalResult("offline", writer_attempted=True)

    async def second_writer(*_args, **_kwargs):
        raise AssertionError("a failed optimal settings write must not be retried")

    async def discovered(*_args, **_kwargs):
        return {"ok": True}

    async def no_auto_load(*_args, **_kwargs):
        return None

    monkeypatch.setattr(local_settings_download, "_managed_download", managed_download)
    monkeypatch.setattr(local_setup_runtime, "apply_optimal_settings", failed_optimal)
    monkeypatch.setattr(local_setup_runtime, "configure_model_settings", second_writer)
    monkeypatch.setattr("llm.catalog_network_models.discover_network_models", discovered)
    monkeypatch.setattr(local_settings_download, "_auto_load", no_auto_load)
    monkeypatch.setattr(local_settings_download.local_downloads, "write", lambda _download: None)
    monkeypatch.setattr(own_omlx_management, "uses_mine", lambda: False)

    result = await local_settings_download.run(lambda _stage: None, model)

    assert result["ok"] is True


async def _empty_settings() -> dict:
    return {}
