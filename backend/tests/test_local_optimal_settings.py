from __future__ import annotations

import httpx
import pytest

from llm import own_omlx_management
from llm.local_downloads import Download
from llm.local_model import LocalModel
from llm import local_setup_auth, local_setup_runtime


@pytest.mark.asyncio
async def test_use_mine_keeps_omlx_settings_byte_identical(monkeypatch) -> None:
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


@pytest.mark.asyncio
@pytest.mark.parametrize(("status_code", "expected"), [(404, "not_found"), (503, "offline")])
async def test_missing_or_unavailable_candidate_never_writes(monkeypatch, status_code, expected) -> None:
    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(own_omlx_management, "uses_mine", lambda: False)
    monkeypatch.setattr(local_setup_auth, "omlx_version", lambda: "0.7.1rc1")

    async def admin_request(_cfg, method, path, **_kwargs):
        calls.append((method, path))
        return _Response(status_code, {})

    from llm.providers import omlx_endpoints
    monkeypatch.setattr(omlx_endpoints, "admin_request", admin_request)

    result = await local_setup_runtime.apply_optimal_settings("server", model)

    assert result.status == expected
    assert calls == [("GET", f"/models/{model.model}/settings/optimal")]


@pytest.mark.asyncio
async def test_unreachable_omlx_returns_no_omlx_without_a_writer(monkeypatch) -> None:
    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768)
    monkeypatch.setattr(own_omlx_management, "uses_mine", lambda: False)
    monkeypatch.setattr(local_setup_auth, "omlx_version", lambda: "0.7.1rc1")

    async def unreachable(*_args, **_kwargs):
        from llm.providers.omlx_endpoints import OmlxAdminError
        raise OmlxAdminError("offline")

    from llm.providers import omlx_endpoints
    monkeypatch.setattr(omlx_endpoints, "admin_request", unreachable)

    result = await local_setup_runtime.apply_optimal_settings("server", model)

    assert result.status == "no_omlx"


class _Response:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> dict:
        return self._body


@pytest.mark.asyncio
@pytest.mark.parametrize("turboquant, expected", [
    ({"turboquant_kv_enabled": True, "turboquant_kv_bits": 4}, {"turboquant_kv_enabled": True, "turboquant_kv_bits": 4}),
    # A benchmark that ran without TurboQuant keeps it off: CORSIA's default must not switch it on.
    ({"turboquant_kv_enabled": False, "turboquant_kv_bits": 4}, {}),
])
async def test_applied_optimal_settings_use_one_filtered_writer(monkeypatch, turboquant, expected) -> None:
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
            return _Response(200, {"model_specific": ["turboquant_kv_enabled", "turboquant_kv_bits", "dflash_draft_model", "prefill_step_size"]})
        if path == "/models":
            return _Response(200, {"models": [{"id": model.model}]})
        return _Response(200, {})

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def get(self, _url):
            return _Response(200, {"model_settings": {**turboquant, "prefill_step_size": 1024, "dflash_draft_model": "missing", "temperature": 0.7}})

    from llm.providers import omlx_endpoints
    monkeypatch.setattr(omlx_endpoints, "admin_request", admin_request)
    monkeypatch.setattr(local_setup_runtime.httpx, "AsyncClient", lambda **_kwargs: Client())

    result = await local_setup_runtime.apply_optimal_settings("server", model)

    assert result.status == "applied"
    assert result.speed == 52.0
    assert len(writes) == 1
    assert {key: value for key, value in writes[0].items() if key.startswith("turboquant_")} == expected
    assert writes[0]["prefill_step_size"] == 1024
    assert "dflash_draft_model" not in writes[0]
    assert "temperature" not in writes[0]


@pytest.mark.asyncio
async def test_draft_lookup_failure_is_offline_and_does_not_fail_the_download(monkeypatch) -> None:
    from llm import local_settings_download

    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768)
    configured: list[str] = []
    monkeypatch.setattr(own_omlx_management, "uses_mine", lambda: False)
    monkeypatch.setattr(local_setup_auth, "omlx_version", lambda: "0.7.1rc1")
    monkeypatch.setattr(
        local_setup_runtime,
        "_read_optimal_inputs",
        lambda *_args: _optimal_inputs(),
    )

    async def unavailable_drafts(*_args):
        raise httpx.ConnectError("offline")

    async def managed_download(*_args, **_kwargs):
        return "server", True

    async def configure(server_id, _model):
        configured.append(server_id)

    async def discovered(_server_id):
        return {"ok": True}

    async def no_auto_load(*_args, **_kwargs):
        return None

    monkeypatch.setattr(local_setup_runtime, "_without_uninstalled_drafts", unavailable_drafts)
    monkeypatch.setattr(local_settings_download, "_managed_download", managed_download)
    monkeypatch.setattr(local_setup_runtime, "configure_model_settings", configure)
    monkeypatch.setattr("llm.catalog_network_models.discover_network_models", discovered)
    monkeypatch.setattr(local_settings_download, "_auto_load", no_auto_load)
    monkeypatch.setattr(local_settings_download.local_downloads, "write", lambda _download: None)

    assert (await local_setup_runtime.apply_optimal_settings("server", model)).status == "offline"
    assert (await local_settings_download.run(lambda _stage: None, model))["ok"] is True
    assert configured == ["server"]


async def _optimal_inputs():
    return ({"tg_tps": 52.0}, {"model_settings": {"dflash_draft_model": "draft"}}, {"model_specific": ["dflash_draft_model"]}, {})


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


@pytest.mark.asyncio
async def test_auto_load_only_runs_when_the_memory_gate_allows(monkeypatch, caplog) -> None:
    from llm import local_settings_download

    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768)
    loaded: list[str] = []

    async def admin_request(_cfg, method, path, **_kwargs):
        assert (method, path) == ("GET", "/models")
        return _Response(200, {"models": [{"id": model.model, "estimated_size": 4 * 1024**3}]})

    async def cannot_load(*_args, **_kwargs):
        return {"ok": False}

    async def load_model(*_args, **_kwargs):
        loaded.append(model.model)
        return {"ok": True}

    monkeypatch.setattr(local_settings_download, "notify_cached_model_loaded", lambda *_args: None)
    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)
    monkeypatch.setattr("infra.host_resources.check_can_load", cannot_load)
    monkeypatch.setattr("llm.providers.omlx_lifecycle.load_model", load_model)

    await local_settings_download._auto_load("server", model, allowed=True)

    assert loaded == []
    assert "not enough free memory" in caplog.text


@pytest.mark.asyncio
async def test_auto_load_logs_when_management_disallows_it(monkeypatch, caplog) -> None:
    from llm import local_settings_download

    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768)
    await local_settings_download._auto_load("server", model, allowed=False)

    assert "does not manage this oMLX" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("response", "expected"), [
    (_Response(503, {}), "models request returned 503"),
    (_Response(200, {"models": []}), "downloaded model is missing"),
    (_Response(200, {"models": [{"id": "Qwen3.8-27B-oQ4e", "estimated_size": 0}]}), "no usable size"),
])
async def test_auto_load_logs_unusable_model_state(monkeypatch, caplog, response, expected) -> None:
    from llm import local_settings_download

    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768)

    async def admin_request(*_args, **_kwargs):
        return response

    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)
    await local_settings_download._auto_load("server", model, allowed=True)

    assert expected in caplog.text


@pytest.mark.asyncio
async def test_auto_load_uses_existing_loader_when_the_model_fits(monkeypatch) -> None:
    from llm import local_settings_download

    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768)
    loaded: list[str] = []

    async def admin_request(_cfg, _method, _path, **_kwargs):
        return _Response(200, {"models": [{"id": model.model, "estimated_size": 4 * 1024**3}]})

    async def can_load(*_args, **_kwargs):
        return {"ok": True}

    async def load_model(_config, model_id):
        loaded.append(model_id)
        return {"ok": True}

    async def discovered(_server_id):
        return None

    monkeypatch.setattr(local_settings_download, "notify_cached_model_loaded", lambda *_args: None)
    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)
    monkeypatch.setattr("infra.host_resources.check_can_load", can_load)
    monkeypatch.setattr("llm.providers.omlx_lifecycle.load_model", load_model)
    monkeypatch.setattr("llm.network_discovery.discover_and_notify", discovered)

    await local_settings_download._auto_load("server", model, allowed=True)

    assert loaded == [model.model]


@pytest.mark.asyncio
async def test_auto_load_failure_keeps_the_completed_download_available(monkeypatch, caplog) -> None:
    from llm import local_settings_download

    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768)
    downloads: list[Download] = []

    async def managed_download(*_args, **_kwargs):
        return "server", True

    async def optimal(*_args, **_kwargs):
        return local_setup_runtime.OptimalResult("applied")

    async def discovered(_server_id):
        return {"ok": True}

    async def admin_request(*_args, **_kwargs):
        return _Response(200, {"models": [{"id": model.model, "estimated_size": 4 * 1024**3}]})

    async def can_load(*_args, **_kwargs):
        return {"ok": True}

    async def failed_load(*_args, **_kwargs):
        raise RuntimeError("load failed")

    monkeypatch.setattr(local_settings_download, "_managed_download", managed_download)
    monkeypatch.setattr(local_setup_runtime, "apply_optimal_settings", optimal)
    monkeypatch.setattr("llm.catalog_network_models.discover_network_models", discovered)
    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)
    monkeypatch.setattr("infra.host_resources.check_can_load", can_load)
    monkeypatch.setattr("llm.providers.omlx_lifecycle.load_model", failed_load)
    monkeypatch.setattr(local_settings_download, "notify_cached_model_loaded", lambda *_args: None)
    monkeypatch.setattr(local_settings_download.local_downloads, "write", downloads.append)
    monkeypatch.setattr(own_omlx_management, "uses_mine", lambda: False)

    assert (await local_settings_download.run(lambda _stage: None, model))["ok"] is True
    assert downloads[0].status == "available"
    assert "model load failed" in caplog.text


@pytest.mark.asyncio
async def test_settings_download_never_auto_loads_under_use_mine(monkeypatch) -> None:
    from llm import local_settings_download

    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768)
    auto_load_calls: list[bool] = []
    downloads: list[Download] = []

    async def managed_download(*_args, **_kwargs):
        return "server", True

    async def optimal(*_args, **_kwargs):
        return local_setup_runtime.OptimalResult("applied")

    async def discovered(*_args, **_kwargs):
        return {"ok": True}

    async def auto_load(_server_id, _model, *, allowed):
        auto_load_calls.append(allowed)

    monkeypatch.setattr(local_settings_download, "_managed_download", managed_download)
    monkeypatch.setattr(local_setup_runtime, "apply_optimal_settings", optimal)
    monkeypatch.setattr("llm.catalog_network_models.discover_network_models", discovered)
    monkeypatch.setattr(local_settings_download, "_auto_load", auto_load)
    monkeypatch.setattr(local_settings_download.local_downloads, "write", downloads.append)
    monkeypatch.setattr(own_omlx_management, "uses_mine", lambda: True)

    result = await local_settings_download.run(lambda _stage: None, model)

    assert result["ok"] is True
    assert auto_load_calls == [False]
    assert len(downloads) == 1
    assert downloads[0].server_id == "server"


@pytest.mark.asyncio
async def test_settings_flight_emits_completed_event(monkeypatch) -> None:
    from llm import local_settings_download, local_setup

    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768)
    emitted: list[tuple[str, dict]] = []

    async def completed(_report, _model):
        return {"ok": True}

    monkeypatch.setattr(local_settings_download, "run", completed)
    monkeypatch.setattr(local_setup, "_emit", lambda action, **properties: emitted.append((action, properties)))
    monkeypatch.setattr(local_setup.local_setup_operation, "finish", lambda _flight, result: result)

    assert await local_settings_download._run_flight(model) == {"ok": True}
    assert emitted == [("completed", {"kind": "settings", "model": model.repository})]


@pytest.mark.asyncio
async def test_settings_flight_emits_failed_event(monkeypatch) -> None:
    from llm import local_settings_download, local_setup

    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768)
    emitted: list[tuple[str, dict]] = []

    async def failed(_report, _model):
        raise ValueError("oMLX unavailable")

    monkeypatch.setattr(local_settings_download, "run", failed)
    monkeypatch.setattr(local_settings_download.local_downloads, "write", lambda _download: None)
    monkeypatch.setattr(local_setup, "_emit", lambda action, **properties: emitted.append((action, properties)))
    monkeypatch.setattr(local_setup.local_setup_operation, "finish", lambda _flight, result: result)

    result = await local_settings_download._run_flight(model)

    assert result["ok"] is False
    assert emitted == [("failed", {"kind": "settings", "model": model.repository, "error_type": "ValueError"})]


async def _empty_settings() -> dict:
    return {}
