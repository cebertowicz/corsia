"""Lifecycle and daemon settings routes for network servers."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from infra.db_async import run_in_db
from infra.product_events import emit_product_event
from infra.state.ws import notify_models_changed
from llm import network_discovery as nd
from llm.providers import for_provider

router = APIRouter(tags=["network-servers"])


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
        return {
            "ok": False,
            "message": result.get("error", "Reload failed"),
        }

    try:
        provider_module = for_provider(provider)
    except ValueError:
        return {
            "ok": False,
            "message": f"No lifecycle provider module for provider '{provider}'",
        }

    cfg = {"server_id": server_id}
    if action == "start":
        result = await provider_module.start_server(cfg, health_timeout_s=30.0)
    else:
        result = await provider_module.stop_server(cfg)

    if result.get("ok"):
        if action == "start":
            await run_in_db(nd.update_server_status, server_id, status="online")
        elif action == "stop":
            await run_in_db(nd.update_server_status, server_id, status="offline")

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
        raise HTTPException(status if status < 500 else 502, detail=result.get("message"))
    if "pinned" in body:
        action = "pin" if body["pinned"] else "unpin"
        emit_product_event(
            "model", action,
            properties={
                "provider": server["provider"], "model_id": model_id,
                "server_id": server_id,
            },
        )
    return {k: v for k, v in result.items() if k != "ok"}
