"""Lifecycle and daemon settings routes for network servers."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from infra.db_async import run_in_db
from infra.failure_reference import new_reference
from infra.product_events import emit_product_event
from infra.state.ws import notify_models_changed
from llm import network_discovery as nd
from llm.providers import for_provider

router = APIRouter(tags=["network-servers"])
log = logging.getLogger(__name__)


def _remember_managed_choice(server: dict, running: bool) -> None:
    """The person's Start or Stop on CORSIA's own local model decides whether startup runs it."""
    from infra import settings
    from llm.managed_omlx_binary import is_corsia_managed_server

    if is_corsia_managed_server(server):
        settings.save_setting_merge("providers", {server["provider"]: {"autoStart": running}})


def _reload_failure(result: dict) -> dict:
    ref = new_reference("local")
    log.warning("Model list refresh failed reference=%s: %s", ref, result.get("error"))
    return {"ok": False, "message": "Couldn’t refresh the model list. Try again.", "ref": ref}


async def _confirm_low_memory_start(server_id: str, provider_module, cfg: dict) -> dict | None:
    """Confirm a model after starting CORSIA's stopped server; never download one."""
    from llm.catalog import discover_network_models
    from llm.local_sources import catalog_membership

    refreshed = await discover_network_models(server_id)
    if not refreshed.get("ok"):
        return _reload_failure(refreshed)
    available, _enabled = await asyncio.to_thread(catalog_membership)
    if f"omlx@{server_id}" in available:
        return None
    stopped = await provider_module.stop_server(cfg)
    if not stopped.get("ok"):
        ref = new_reference("local")
        message = stopped.get("message") or stopped.get("error") or "Couldn’t stop the local model."
        log.warning("Local server rollback failed reference=%s: %s", ref, message)
        return {**stopped, "message": message, "ref": ref}
    await run_in_db(nd.update_server_status, server_id, status="offline")
    return {"ok": False, "reason": "mac_cannot_run", "message": (
        "Local models need at least 32 GB of memory, and this computer has less. "
        "Smaller models and future updates may bring them to computers with less memory. "
        "Connect a subscription or an API provider.")}


@router.post("/{server_id}/lifecycle/{action}")
async def lifecycle_action(server_id: str, action: str):
    if action not in ("start", "stop", "reload"):
        raise HTTPException(400, detail=f"Invalid lifecycle action: {action}")

    server = await run_in_db(nd.get_server, server_id)
    if not server:
        raise HTTPException(404, detail="Server not found")

    provider = server.get("provider", "")

    if action == "reload":
        from llm.catalog import discover_network_models
        result = await discover_network_models(server_id)
        if result.get("ok"):
            notify_models_changed()
            return {
                "ok": True,
                "message": f"Discovered {result.get('discovered', 0)} model(s)",
                **{k: v for k, v in result.items() if k != "ok"},
            }
        return _reload_failure(result)

    try:
        provider_module = for_provider(provider)
    except ValueError:
        return {
            "ok": False,
            "message": f"No lifecycle provider module for provider '{provider}'",
        }

    cfg = {"server_id": server_id}
    if action == "start":
        result = await provider_module.start_server(cfg)
    else:
        result = await provider_module.stop_server(cfg)

    if result.get("ok") and action == "start":
        # Discovery reads the persisted status and refuses an offline server.
        await run_in_db(nd.update_server_status, server_id, status="online")
        from llm.local_setup_gates import local_setup_memory_ok
        from llm.managed_omlx_binary import is_corsia_managed_server

        if is_corsia_managed_server(server) and not await asyncio.to_thread(local_setup_memory_ok):
            failure = await _confirm_low_memory_start(server_id, provider_module, cfg)
            if failure is not None:
                return failure

    if result.get("ok"):
        if action == "stop":
            await run_in_db(nd.update_server_status, server_id, status="offline")
        await run_in_db(_remember_managed_choice, server, action == "start")
    else:
        # The reference ties the failure the person sees to this line, which carries its cause.
        error = result.pop("error", None) or result.get("message")
        result = {**result, "message": result.get("message") or error, "ref": new_reference("local")}
        log.warning("Local server %s failed reference=%s: %s", action, result["ref"], error)

    return result


@router.get("/{server_id}/settings")
async def get_daemon_settings(server_id: str):
    server = await run_in_db(nd.get_server, server_id)
    if not server:
        raise HTTPException(404, detail="Server not found")
    cfg = {"server_id": server_id}
    provider_module = for_provider(server["provider"])
    result = await provider_module.get_settings(cfg)
    if not result.get("ok"):
        raise HTTPException(503, detail=result.get("message", "Daemon unreachable"))
    return {k: v for k, v in result.items() if k != "ok"}


@router.get("/{server_id}/models/{model_id:path}/runtime-info")
async def get_daemon_model_runtime_info(
    server_id: str, model_id: str,
):
    server = await run_in_db(nd.get_server, server_id)
    if not server:
        raise HTTPException(404, detail="Server not found")
    cfg = {"server_id": server_id}
    provider_module = for_provider(server["provider"])
    if not hasattr(provider_module, "get_model_runtime_info"):
        raise HTTPException(
            400, detail=f"{server['provider']} doesn't support per-model runtime info",
        )
    result = await provider_module.get_model_runtime_info(cfg, model_id)
    if not result.get("ok"):
        raise HTTPException(404, detail=result.get("message", "Not found"))
    return {k: v for k, v in result.items() if k != "ok"}


@router.put("/{server_id}/models/{model_id:path}/settings")
async def update_daemon_model_settings(
    server_id: str, model_id: str, body: dict,
):
    server = await run_in_db(nd.get_server, server_id)
    if not server:
        raise HTTPException(404, detail="Server not found")
    if not isinstance(body, dict) or not body:
        raise HTTPException(400, detail="Body must be a non-empty JSON object")
    cfg = {"server_id": server_id}
    provider_module = for_provider(server["provider"])
    result = await provider_module.update_model_settings(cfg, model_id, body)
    if not result.get("ok"):
        status = result.get("status", 502)
        log.warning("Could not save model settings: %s", result.get("message"))
        raise HTTPException(status if status < 500 else 502, detail="Couldn’t save this model’s settings. Try again.")
    if "pinned" in body:
        from llm.local_keep_loaded import forget_failure

        forget_failure(server_id, model_id)
        action = "pin" if body["pinned"] else "unpin"
        emit_product_event(
            "model", action,
            properties={
                "provider": server["provider"], "model_id": model_id,
                "server_id": server_id,
            },
        )
    return {k: v for k, v in result.items() if k != "ok"}
