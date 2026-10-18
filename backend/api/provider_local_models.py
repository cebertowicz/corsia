"""Settings routes for adding and removing a local oMLX model."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from infra.auth import require_user_header_only
from infra.db_async import run_in_db
from llm import local_downloads, local_setup_operation
from llm.local_model import OMLX_PIN, LocalModel, PickUnavailable, ask_recommendations, omlx_has_recommender
from llm.local_setup_auth import omlx_version
from llm.local_setup_gates import local_memory_ok
from llm.local_setup_download import download_dir
from llm.local_setup_ownership import download_disk_space
from llm.network_discovery import get_servers

router = APIRouter(prefix="/api/providers/local", tags=["providers"])


async def _server() -> dict[str, Any]:
    servers = [server for server in await run_in_db(get_servers, provider="omlx")
               if server.get("kind") != "external" and server.get("status") == "online"]
    if len(servers) != 1:
        raise HTTPException(409, "CORSIA could not reach one local model server.")
    return servers[0]


async def _recommendations() -> tuple[dict[str, Any], dict[str, list[LocalModel]]]:
    downloads = [item.view() for item in local_downloads.all()]
    if not await asyncio.to_thread(local_memory_ok):
        from llm.local_setup_prepare import NO_LOCAL_MODEL

        return {"state": "low_memory", "message": NO_LOCAL_MODEL, "sections": {}, "downloads": downloads}, {}
    server = await _server()
    if not omlx_has_recommender(omlx_version()):
        return {
            "state": "update_required",
            "message": f"Update oMLX to {OMLX_PIN} or later to add a local model.",
            "sections": {}, "downloads": downloads,
        }, {}
    try:
        sections = await ask_recommendations(str(server["id"]))
    except PickUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    model_dir = await download_dir(str(server["id"]))

    def model_view(model: LocalModel) -> dict[str, Any]:
        space = download_disk_space(model.download_size_bytes, model_dir, model.repository)
        return {**model.view(), "disk": space}

    return {
        "state": "ready",
        "sections": {name: [model_view(model) for model in models] for name, models in sections.items()},
        "downloads": downloads,
    }, sections


@router.get("/recommendations")
async def recommendations(_user: dict = Depends(require_user_header_only)) -> dict[str, Any]:
    result, _sections = await _recommendations()
    return result


@router.post("/downloads")
async def download_model(body: dict = Body(...), _user: dict = Depends(require_user_header_only)) -> dict[str, Any]:
    repository = body.get("repository") if isinstance(body, dict) and set(body) == {"repository"} else None
    if not isinstance(repository, str) or not repository:
        raise HTTPException(400, "A recommended model repository is required.")
    _view, sections = await _recommendations()
    model = next((item for rows in sections.values() for item in rows if item.repository == repository), None)
    if model is None:
        raise HTTPException(400, "That model is not currently recommended for this Mac.")
    server = await _server()
    model_dir = await download_dir(str(server["id"]))
    space = download_disk_space(model.download_size_bytes, model_dir, model.repository)
    if space["state"] == "unknown":
        raise HTTPException(409, "CORSIA could not determine free disk space for this download.")
    if space["state"] == "insufficient":
        raise HTTPException(409, "This Mac does not have enough free disk space for this download.")
    operation = local_setup_operation.read()
    if operation is not None:
        raise HTTPException(409, f"{operation.model.display_name} is already downloading.")
    from llm.local_setup import begin_settings_download

    return await begin_settings_download(model)


@router.delete("/downloads/{repository:path}")
async def delete_download(repository: str, _user: dict = Depends(require_user_header_only)) -> dict[str, Any]:
    downloaded = local_downloads.get(repository)
    if downloaded is None:
        raise HTTPException(404, "This Settings download is not known to CORSIA.")
    operation = local_setup_operation.read()
    if operation is not None and operation.repository == repository:
        raise HTTPException(409, "This model has an active download.")
    from llm.local_model import set_up_model
    setup = set_up_model()
    if setup is not None and setup.repository == repository:
        raise HTTPException(409, "The model set up by CORSIA cannot be deleted here.")
    server = await _server()
    from llm.providers.omlx_lifecycle import list_loaded
    loaded = await list_loaded({"server_id": server["id"]})
    if any(row.get("identifier") == downloaded.model.model for row in loaded):
        raise HTTPException(409, "Unload this model before deleting its download.")
    from llm.providers.omlx_endpoints import admin_request
    response = await admin_request({"server_id": server["id"]}, "DELETE", f"/hf/models/{downloaded.model.model}", timeout=30.0)
    if response.status_code != 200:
        raise HTTPException(502, "oMLX could not delete this model download.")
    from llm.catalog_network_models import discover_network_models
    refreshed = await discover_network_models(str(server["id"]))
    if not refreshed.get("ok"):
        raise HTTPException(502, "CORSIA could not refresh the local model catalog after deletion.")
    confirmed = await admin_request({"server_id": server["id"]}, "GET", "/models", timeout=15.0)
    if confirmed.status_code != 200:
        raise HTTPException(502, "CORSIA could not confirm the local model was deleted.")
    payload = confirmed.json()
    models = payload.get("models") if isinstance(payload, dict) else None
    if any(isinstance(row, dict) and row.get("id") == downloaded.model.model for row in models or []):
        raise HTTPException(502, "oMLX did not remove this model download.")
    local_downloads.remove(repository)
    from llm.own_omlx_management import forget_model_originals
    forget_model_originals(downloaded.model.model)
    from infra.state.ws import notify_models_changed
    notify_models_changed()
    return {"ok": True}
