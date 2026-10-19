from __future__ import annotations

import pytest
from fastapi import HTTPException

from api import provider_local_models
from llm import local_downloads, local_setup_operation
from llm.local_model import LocalModel


MODEL = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768, level=4.0, fp16=False, relative_speed=0.85)


class _Response:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> dict:
        return self._body


async def _server_in_db(_function, *_args, **_kwargs):
    return [{"id": "server", "kind": "managed", "status": "online"}]


async def _no_loaded(_config):
    return []


async def _known_disk_model(_config):
    return [{"identifier": MODEL.model}]


def _route_with_known_model(monkeypatch) -> None:
    monkeypatch.setattr(provider_local_models, "run_in_db", _server_in_db)
    monkeypatch.setattr("llm.providers.omlx_lifecycle.list_loaded", _no_loaded)
    monkeypatch.setattr("llm.providers.omlx_lifecycle.list_disk_models", _known_disk_model)
    monkeypatch.setattr("llm.local_model.set_up_model", lambda: None)
    monkeypatch.setattr(provider_local_models.local_setup_operation, "read", lambda: None)


@pytest.mark.asyncio
async def test_delete_known_omlx_model_without_settings_record(monkeypatch, tmp_path) -> None:
    _route_with_known_model(monkeypatch)
    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    requests: list[tuple[str, str]] = []

    async def admin_request(_config, method, path, **_kwargs):
        requests.append((method, path))
        return _Response(200, {} if method == "DELETE" else {"models": []})

    async def discovered(_server_id):
        return {"ok": True}

    forgotten: list[str] = []
    changed: list[bool] = []
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)
    monkeypatch.setattr("llm.catalog_network_models.discover_network_models", discovered)
    monkeypatch.setattr("llm.own_omlx_management.forget_model_originals", forgotten.append)
    monkeypatch.setattr("llm.local_setup._emit", lambda action, **properties: emitted.append((action, properties)))
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: changed.append(True))

    assert await provider_local_models.delete_download(MODEL.repository, {}) == {"ok": True}
    assert requests == [("DELETE", f"/hf/models/{MODEL.model}"), ("GET", "/models")]
    assert forgotten == [MODEL.model]
    assert emitted == [("deleted", {"kind": "settings", "model": MODEL.model})]
    assert changed == [True]


@pytest.mark.asyncio
async def test_delete_clears_an_available_record_missing_from_disk(monkeypatch, tmp_path) -> None:
    _route_with_known_model(monkeypatch)
    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    local_downloads.write(local_downloads.Download(MODEL))

    async def missing_from_disk(_config):
        return []

    forgotten: list[str] = []
    changed: list[bool] = []
    monkeypatch.setattr("llm.providers.omlx_lifecycle.list_disk_models", missing_from_disk)
    monkeypatch.setattr("llm.own_omlx_management.forget_model_originals", forgotten.append)
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: changed.append(True))

    assert await provider_local_models.delete_download(MODEL.repository, {}) == {"ok": True}
    assert local_downloads.get(MODEL.model) is None
    assert forgotten == [MODEL.model]
    assert changed == [True]


@pytest.mark.asyncio
async def test_delete_removes_durable_settings_record_after_catalog_confirms_absence(monkeypatch, tmp_path) -> None:
    _route_with_known_model(monkeypatch)
    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    local_downloads.write(local_downloads.Download(MODEL))

    async def admin_request(_config, method, _path, **_kwargs):
        return _Response(200, {} if method == "DELETE" else {"models": []})

    async def discovered(_server_id):
        return {"ok": True}

    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)
    monkeypatch.setattr("llm.catalog_network_models.discover_network_models", discovered)
    monkeypatch.setattr("llm.own_omlx_management.forget_model_originals", lambda _model: None)
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: None)

    assert await provider_local_models.delete_download(MODEL.repository, {}) == {"ok": True}
    assert local_downloads.get(MODEL.model) is None


@pytest.mark.asyncio
async def test_delete_keeps_durable_row_until_catalog_confirms_absence(monkeypatch, tmp_path) -> None:
    _route_with_known_model(monkeypatch)
    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    local_downloads.write(local_downloads.Download(MODEL))

    async def admin_request(_config, method, _path, **_kwargs):
        return _Response(200, {} if method == "DELETE" else {"models": [{"id": MODEL.model}]})

    async def discovered(_server_id):
        return {"ok": True}

    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)
    monkeypatch.setattr("llm.catalog_network_models.discover_network_models", discovered)

    with pytest.raises(HTTPException, match="Couldn't delete"):
        await provider_local_models.delete_download(MODEL.repository, {})

    assert local_downloads.get(MODEL.model) is not None


@pytest.mark.asyncio
async def test_delete_refuses_loaded_model(monkeypatch) -> None:
    _route_with_known_model(monkeypatch)

    async def loaded(_config):
        return [{"identifier": MODEL.model}]

    monkeypatch.setattr("llm.providers.omlx_lifecycle.list_loaded", loaded)

    with pytest.raises(HTTPException, match="Couldn't delete this model while it is loaded") as raised:
        await provider_local_models.delete_download(MODEL.repository, {})

    assert raised.value.status_code == 409


@pytest.mark.asyncio
async def test_delete_refuses_actual_corsia_setup_model(monkeypatch) -> None:
    _route_with_known_model(monkeypatch)
    monkeypatch.setattr("llm.local_model.set_up_model", lambda: MODEL)

    with pytest.raises(HTTPException) as raised:
        await provider_local_models.delete_download(MODEL.repository, {})

    assert raised.value.status_code == 409
    assert raised.value.detail == "Couldn't delete this model: CORSIA keeps it to run when no other model is available."


@pytest.mark.asyncio
async def test_delete_refuses_active_or_recoverable_operation_for_model(monkeypatch) -> None:
    _route_with_known_model(monkeypatch)
    monkeypatch.setattr(
        provider_local_models.local_setup_operation,
        "read",
        lambda: local_setup_operation.Operation.for_model(MODEL, "settings", "resume-task"),
    )

    with pytest.raises(HTTPException, match="Couldn't delete this model while it is downloading") as raised:
        await provider_local_models.delete_download(MODEL.repository, {})

    assert raised.value.status_code == 409


@pytest.mark.asyncio
async def test_recommendations_remove_available_records_missing_from_disk(monkeypatch, tmp_path) -> None:
    async def server():
        return {"id": "server"}

    async def missing_from_disk(_config):
        return []

    forgotten: list[str] = []
    failed_model = LocalModel("Jundot/Other", 1_000_000, 32_768)
    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    local_downloads.write(local_downloads.Download(MODEL))
    local_downloads.write(local_downloads.Download(failed_model, status="failed"))
    monkeypatch.setattr(provider_local_models, "local_memory_ok", lambda: True)
    monkeypatch.setattr(provider_local_models, "_server", server)
    monkeypatch.setattr(provider_local_models, "omlx_has_recommender", lambda _version: False)
    monkeypatch.setattr(provider_local_models, "omlx_version", lambda: "0.7.0")
    monkeypatch.setattr("llm.providers.omlx_lifecycle.list_disk_models", missing_from_disk)
    monkeypatch.setattr("llm.own_omlx_management.forget_model_originals", forgotten.append)

    result, _sections = await provider_local_models._recommendations()

    assert result["downloads"] == [local_downloads.Download(failed_model, status="failed").view()]
    assert local_downloads.get(MODEL.model) is None
    assert local_downloads.get(failed_model.model) is not None
    assert forgotten == [MODEL.model]


@pytest.mark.asyncio
async def test_recommendations_leave_records_when_disk_listing_fails(monkeypatch, tmp_path) -> None:
    async def server():
        return {"id": "server"}

    async def unavailable_disk_listing(_config):
        raise RuntimeError("offline")

    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    local_downloads.write(local_downloads.Download(MODEL))
    monkeypatch.setattr(provider_local_models, "local_memory_ok", lambda: True)
    monkeypatch.setattr(provider_local_models, "_server", server)
    monkeypatch.setattr(provider_local_models, "omlx_has_recommender", lambda _version: False)
    monkeypatch.setattr(provider_local_models, "omlx_version", lambda: "0.7.0")
    monkeypatch.setattr("llm.providers.omlx_lifecycle.list_disk_models", unavailable_disk_listing)

    result, _sections = await provider_local_models._recommendations()

    assert result["downloads"] == [local_downloads.Download(MODEL).view()]
    assert local_downloads.get(MODEL.model) is not None


@pytest.mark.asyncio
async def test_recommendations_include_memory_and_download_volume_free_disk(monkeypatch, tmp_path) -> None:
    async def recommended(_server_id):
        return {"best": [MODEL]}

    async def server():
        return {"id": "server"}

    async def model_dir(_server_id):
        return tmp_path

    async def no_disk_models(_config):
        return []

    monkeypatch.setattr(provider_local_models, "local_memory_ok", lambda: True)
    monkeypatch.setattr(provider_local_models, "host_ram_bytes", lambda: 64 * 1024**3)
    monkeypatch.setattr(provider_local_models, "omlx_has_recommender", lambda _version: True)
    monkeypatch.setattr(provider_local_models, "omlx_version", lambda: "0.7.1")
    monkeypatch.setattr(provider_local_models, "_server", server)
    monkeypatch.setattr("llm.providers.omlx_lifecycle.list_disk_models", no_disk_models)
    monkeypatch.setattr(provider_local_models, "ask_recommendations", recommended)
    monkeypatch.setattr(provider_local_models, "download_dir", model_dir)
    monkeypatch.setattr(
        provider_local_models,
        "download_disk_space",
        lambda *_args: {"state": "ready", "free_bytes": 20 * 1000**3},
    )

    result, _sections = await provider_local_models._recommendations()

    assert result["memory_gb"] == 64
    assert result["disk_free_gb"] == 20
    assert result["sections"]["best"] == [{
        **MODEL.view(), "disk": {"state": "ready", "free_bytes": 20 * 1000**3},
    }]


@pytest.mark.asyncio
async def test_download_names_a_model_missing_from_recommendations(monkeypatch) -> None:
    async def no_recommendations():
        return {}, {"best": []}

    monkeypatch.setattr(provider_local_models, "_recommendations", no_recommendations)
    monkeypatch.setattr(provider_local_models, "_clear_failed_record", lambda _model_id: None)

    with pytest.raises(HTTPException) as raised:
        await provider_local_models.download_model({"repository": MODEL.repository}, {})

    assert raised.value.status_code == 400
    assert raised.value.detail == "Couldn't download that model: it isn't in the list for this computer right now. Pick another from the list."


@pytest.mark.asyncio
async def test_download_resumes_the_same_model_operation(monkeypatch) -> None:
    operation = local_setup_operation.Operation.for_model(MODEL, "settings", "resume-task")
    monkeypatch.setattr(provider_local_models, "_recommendations", lambda: _recommendations_for(MODEL))
    monkeypatch.setattr(provider_local_models, "_server", lambda: _model_server())
    monkeypatch.setattr(provider_local_models, "download_dir", lambda _server_id: _model_dir())
    monkeypatch.setattr(provider_local_models, "download_disk_space", lambda *_args: {"state": "ready"})
    monkeypatch.setattr(provider_local_models.local_setup_operation, "read", lambda: operation)
    resumed: list[LocalModel] = []

    async def begin(model):
        resumed.append(model)
        return {"ok": True, "resumed": True}

    monkeypatch.setattr("llm.local_setup.begin_settings_download", begin)

    assert await provider_local_models.download_model({"repository": MODEL.repository}, {}) == {"ok": True, "resumed": True}
    assert resumed == [MODEL]


@pytest.mark.asyncio
async def test_download_refuses_another_model_operation(monkeypatch) -> None:
    other = LocalModel("Jundot/Other", 1_000_000, 32_768)
    monkeypatch.setattr(provider_local_models, "_recommendations", lambda: _recommendations_for(MODEL))
    monkeypatch.setattr(provider_local_models, "_server", lambda: _model_server())
    monkeypatch.setattr(provider_local_models, "download_dir", lambda _server_id: _model_dir())
    monkeypatch.setattr(provider_local_models, "download_disk_space", lambda *_args: {"state": "ready"})
    monkeypatch.setattr(
        provider_local_models.local_setup_operation,
        "read",
        lambda: local_setup_operation.Operation.for_model(other, "settings", "other-task"),
    )

    with pytest.raises(HTTPException, match="Other is already downloading"):
        await provider_local_models.download_model({"repository": MODEL.repository}, {})


async def _recommendations_for(model: LocalModel):
    return {}, {"best": [model]}


async def _model_server():
    return {"id": "server"}


async def _model_dir():
    from pathlib import Path

    return Path("/tmp")
