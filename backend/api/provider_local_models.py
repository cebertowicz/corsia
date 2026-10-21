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
from llm.local_setup_gates import host_ram_bytes, local_memory_ok
from llm.local_setup_download import download_dir
from llm.local_setup_disk import download_disk_space
from llm.network_discovery import get_servers

router = APIRouter(prefix="/api/providers/local", tags=["providers"])


async def _server() -> dict[str, Any]:
    servers = [server for server in await run_in_db(get_servers, provider="omlx")
               if server.get("kind") != "external" and server.get("status") == "online"]
    if len(servers) != 1:
        raise HTTPException(409, "Couldn't reach the local model server. Check that oMLX is running, then try again.")
    return servers[0]


async def _recommendations() -> tuple[dict[str, Any], dict[str, list[LocalModel]]]:
    if not await asyncio.to_thread(local_memory_ok):
        from llm.local_setup_prepare import NO_LOCAL_MODEL

        downloads = [item.view() for item in local_downloads.all()]
        return {"state": "low_memory", "message": NO_LOCAL_MODEL, "sections": {}, "downloads": downloads}, {}
    server = await _server()
    from llm.providers.omlx_lifecycle import list_disk_models

    try:
        disk_models = await list_disk_models({"server_id": server["id"]})
    except Exception:
        disk_models = None
    if isinstance(disk_models, list):
        local_downloads.reconcile({row["identifier"] for row in disk_models
                                   if isinstance(row, dict) and isinstance(row.get("identifier"), str)})
    downloads = [item.view() for item in local_downloads.all()]
    if not omlx_has_recommender(await asyncio.to_thread(omlx_version)):
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

    views = {name: [model_view(model) for model in models] for name, models in sections.items()}
    disk_free_bytes = next(
        (item["disk"].get("free_bytes") for models in views.values() for item in models
         if isinstance(item["disk"].get("free_bytes"), int)),
        None,
    )
    return {
        "state": "ready",
        "sections": views,
        "downloads": downloads,
        "memory_gb": round(host_ram_bytes() / 1024**3),
        "disk_free_gb": round(disk_free_bytes / 1000**3) if isinstance(disk_free_bytes, int) else None,
    }, sections


@router.get("/recommendations")
async def recommendations(_user: dict = Depends(require_user_header_only)) -> dict[str, Any]:
    result, _sections = await _recommendations()
    return result


@router.get("/downloads")
async def downloads(_user: dict = Depends(require_user_header_only)) -> list[dict[str, Any]]:
    """Settings' cheap durable download projection; it never calls the recommender."""
    return [download.view() for download in local_downloads.all()]


def _clear_failed_record(model_id: str) -> None:
    record = local_downloads.get(model_id)
    if record is not None and record.status == "failed":
        local_downloads.forget(model_id)


@router.post("/downloads")
async def download_model(body: dict = Body(...), _user: dict = Depends(require_user_header_only)) -> dict[str, Any]:
    repository = body.get("repository") if isinstance(body, dict) and set(body) == {"repository"} else None
    if not isinstance(repository, str) or not repository:
        raise HTTPException(400, "A recommended model repository is required.")
    _view, sections = await _recommendations()
    model_id = repository.rsplit("/", 1)[-1]
    model = next((item for rows in sections.values() for item in rows if item.repository == repository), None)
    if model is None:
        _clear_failed_record(model_id)
        raise HTTPException(400, "Couldn't download that model: it isn't in the list for this computer right now. Pick another from the list.")
    server = await _server()
    model_dir = await download_dir(str(server["id"]))
    space = download_disk_space(model.download_size_bytes, model_dir, model.repository)
    if space["state"] == "unknown":
        _clear_failed_record(model_id)
        raise HTTPException(409, "Couldn't check free disk space. Try again.")
    if space["state"] == "insufficient":
        _clear_failed_record(model_id)
        raise HTTPException(409, "This computer does not have enough free disk space. Pick another from the list.")
    operation = local_setup_operation.read()
    if operation is not None and operation.model.model != model_id:
        raise HTTPException(409, f"{operation.model.display_name} is already downloading.")
    from llm.local_setup_flow import begin_settings_download

    return await begin_settings_download(model)


@router.delete("/downloads/{model_id:path}")
async def delete_download(model_id: str, _user: dict = Depends(require_user_header_only)) -> dict[str, Any]:
    # oMLX identifies models by the final repository segment (LocalModel.model).
    # local_downloads is only CORSIA's Settings metadata, not a registry of all
    # models that oMLX has downloaded.
    model_id = model_id.rsplit("/", 1)[-1]
    operation = local_setup_operation.read()
    if operation is not None and operation.model.model == model_id:
        raise HTTPException(409, "Couldn't delete this model while it is downloading. Cancel the download first, then try again.")
    from llm.local_model import set_up_model
    setup = set_up_model()
    if setup is not None and setup.model == model_id:
        raise HTTPException(409, "Couldn't delete this model: CORSIA keeps it to run when no other model is available.")
    server = await _server()
    from llm.providers.omlx_lifecycle import list_disk_models, list_loaded
    loaded = await list_loaded({"server_id": server["id"]})
    if any(row.get("identifier") == model_id for row in loaded):
        raise HTTPException(409, "Couldn't delete this model while it is loaded. Unload it first, then try again.")
    disk_models = await list_disk_models({"server_id": server["id"]})
    if not any(row.get("identifier") == model_id for row in disk_models):
        if local_downloads.get(model_id) is not None:
            local_downloads.forget(model_id)
            from infra.state.ws import notify_models_changed

            notify_models_changed()
            return {"ok": True}
        raise HTTPException(404, "Couldn't find this local model download. Refresh the list and try again.")
    downloaded = local_downloads.get(model_id)
    from llm.providers.omlx_endpoints import admin_request
    response = await admin_request({"server_id": server["id"]}, "DELETE", f"/hf/models/{model_id}", timeout=30.0)
    if response.status_code != 200:
        raise HTTPException(502, "Couldn't delete this local model download. Try again.")
    from llm.catalog_network_models import discover_network_models
    refreshed = await discover_network_models(str(server["id"]))
    if not refreshed.get("ok"):
        raise HTTPException(502, "Couldn't refresh local models after deletion. Try again.")
    confirmed = await admin_request({"server_id": server["id"]}, "GET", "/models", timeout=15.0)
    if confirmed.status_code != 200:
        raise HTTPException(502, "Couldn't confirm the local model was deleted. Refresh the list and try again.")
    payload = confirmed.json()
    models = payload.get("models") if isinstance(payload, dict) else None
    if any(isinstance(row, dict) and row.get("id") == model_id for row in models or []):
        raise HTTPException(502, "Couldn't delete this local model download. Try again.")
    if downloaded is not None:
        local_downloads.forget(model_id)
    from llm.local_setup import _emit
    from infra.state.ws import notify_models_changed

    _emit("deleted", kind="settings", model=model_id)
    notify_models_changed()
    return {"ok": True}
