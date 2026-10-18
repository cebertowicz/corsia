from __future__ import annotations

import pytest
from fastapi import HTTPException

from api import provider_local_models
from llm import local_downloads
from llm.local_model import LocalModel


class _Response:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> dict:
        return self._body


@pytest.mark.asyncio
async def test_delete_keeps_the_durable_row_until_catalog_confirms_absence(monkeypatch, tmp_path) -> None:
    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768)
    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    local_downloads.write(local_downloads.Download(model))

    async def run_in_db(_function, *_args, **_kwargs):
        return [{"id": "server", "kind": "managed", "status": "online"}]

    async def list_loaded(_config):
        return []

    async def admin_request(_config, method, _path, **_kwargs):
        if method == "DELETE":
            return _Response(200, {})
        return _Response(200, {"models": [{"id": model.model}]})

    async def discovered(_server_id):
        return {"ok": True}

    monkeypatch.setattr(provider_local_models, "run_in_db", run_in_db)
    monkeypatch.setattr("llm.providers.omlx_lifecycle.list_loaded", list_loaded)
    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)
    monkeypatch.setattr("llm.catalog_network_models.discover_network_models", discovered)

    with pytest.raises(HTTPException, match="did not remove"):
        await provider_local_models.delete_download(model.repository, {})

    assert local_downloads.get(model.repository) is not None
