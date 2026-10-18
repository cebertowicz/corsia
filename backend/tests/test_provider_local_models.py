from __future__ import annotations

import pytest
from fastapi import HTTPException

from api import provider_local_models
from llm import local_downloads, local_setup_operation
from llm.local_model import LocalModel


MODEL = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768)


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
    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)
    monkeypatch.setattr("llm.catalog_network_models.discover_network_models", discovered)
    monkeypatch.setattr("llm.own_omlx_management.forget_model_originals", forgotten.append)
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: changed.append(True))

    assert await provider_local_models.delete_download(MODEL.repository, {}) == {"ok": True}
    assert requests == [("DELETE", f"/hf/models/{MODEL.model}"), ("GET", "/models")]
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
    assert local_downloads.get(MODEL.repository) is None


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

    with pytest.raises(HTTPException, match="did not remove"):
        await provider_local_models.delete_download(MODEL.repository, {})

    assert local_downloads.get(MODEL.repository) is not None


@pytest.mark.asyncio
async def test_delete_refuses_loaded_model(monkeypatch) -> None:
    _route_with_known_model(monkeypatch)

    async def loaded(_config):
        return [{"identifier": MODEL.model}]

    monkeypatch.setattr("llm.providers.omlx_lifecycle.list_loaded", loaded)

    with pytest.raises(HTTPException, match="Unload this model") as raised:
        await provider_local_models.delete_download(MODEL.repository, {})

    assert raised.value.status_code == 409


@pytest.mark.asyncio
async def test_delete_refuses_actual_corsia_setup_model(monkeypatch) -> None:
    _route_with_known_model(monkeypatch)
    monkeypatch.setattr("llm.local_model.set_up_model", lambda: MODEL)

    with pytest.raises(HTTPException) as raised:
        await provider_local_models.delete_download(MODEL.repository, {})

    assert raised.value.status_code == 409
    assert raised.value.detail == "CORSIA runs this model when nothing else is available."


@pytest.mark.asyncio
async def test_delete_refuses_active_or_recoverable_operation_for_model(monkeypatch) -> None:
    _route_with_known_model(monkeypatch)
    monkeypatch.setattr(
        provider_local_models.local_setup_operation,
        "read",
        lambda: local_setup_operation.Operation.for_model(MODEL, "settings", "resume-task"),
    )

    with pytest.raises(HTTPException, match="active or recoverable") as raised:
        await provider_local_models.delete_download(MODEL.repository, {})

    assert raised.value.status_code == 409
