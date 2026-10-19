"""Network server management routes — list and manage models on remote
oMLX servers.

Per-server operations dispatch by provider via ``llm.providers.for_provider``
so the route layer stays provider-runtime agnostic.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictBool

from api.network_server_billing import billing_views, router as network_server_billing_router
from api.network_server_jobs import router as network_server_jobs_router
from api.network_server_lifecycle import router as network_server_lifecycle_router
from api.key_session_guard import audit_key_write, require_person_for_key
from api.providers import mask_key
from api.network_server_helpers import (
    _add_omlx_server,
    add_external_server,
    list_daemon_models,
    update_external_server,
    notify_cached_model_loaded,
    notify_cached_model_unloaded,
)
from infra.auth import require_user
from infra.db_async import run_in_db
from infra.key_store_servers import server_keys
from infra.product_events import emit_product_event
from infra.state.ws import notify_models_changed
from llm import network_discovery as nd
from llm import network_endpoints as ne
from llm.locality import server_on_this_mac
from llm.providers import for_provider

# Every route here, including the included job and lifecycle routers, can
# change or reveal model servers, so the whole prefix requires a caller.
router = APIRouter(
    prefix="/api/settings/network-servers",
    tags=["network-servers"],
    dependencies=[Depends(require_user)],
)
router.include_router(network_server_billing_router)
router.include_router(network_server_jobs_router)
router.include_router(network_server_lifecycle_router)


# A server's key lives in the key store, never in ``network_servers`` (whose rows are
# ``SELECT *``). The routes below report only its presence plus the tail mask, and still
# strip any credential-shaped field a hand-edited metadata blob might carry.
_SECRET_META_KEYS = ("api_key", "token", "secret")


def _public_server_view(row: dict, keys: dict[str, str] | None = None) -> dict:
    """Return ``row`` without credential-bearing metadata, annotated for the UI."""
    # Unreadable metadata is logged by the one parser; the view then shows none.
    stored_metadata = row.pop("metadata_json", None)
    meta = nd.parse_server_metadata(str(row.get("id")), stored_metadata) or {}
    for field in _SECRET_META_KEYS:
        meta.pop(field, None)
    stored = (keys if keys is not None else server_keys([str(row.get("id"))])).get(str(row.get("id")))
    row["metadata"] = meta
    row["private"] = bool(meta.get("private"))
    row["on_this_mac"] = server_on_this_mac({**row, "metadata_json": stored_metadata})
    row["has_key"] = bool(stored)
    row["masked_key"] = mask_key(stored) if stored else ""
    return row


def _network_view(server_id: str) -> dict:
    active = ne.get_active_endpoint(server_id)
    return {
        "endpoints": ne.get_endpoints(server_id),
        "active_host": active["host"] if active else None,
        "api_base": ne.build_api_base(server_id),
        "mgmt_base": ne.build_mgmt_base(server_id),
    }


@router.get("")
async def list_servers():
    servers = await run_in_db(nd.get_servers)
    billing = await run_in_db(billing_views, [s["id"] for s in servers])
    for s in servers:
        s.update(await run_in_db(_network_view, s["id"]))
        s.update(billing[s["id"]])
    keys = await run_in_db(server_keys, [s["id"] for s in servers])
    return {"servers": [_public_server_view(s, keys) for s in servers]}


class AddServerBody(BaseModel):
    name: str
    hostname: str = ""  # IP or hostname (e.g., 100.64.1.5 or mac-studio.tail12345.ts.net)
    port: int = 8000  # oMLX default
    # Cloud endpoints are identified by a verbatim URL, not host+port: a SaaS base
    # carries a scheme AND a path (e.g.
    # https://host.us-east-1.maas.aliyuncs.com/compatible-mode/v1), neither of
    # which is recoverable from host+port. When set this is the authoritative base
    # and `port` is unused. See shared/providers/EXTERNAL-PROVIDER-CACHE-TTL.md.
    base_url: str | None = None
    dialect: str | None = None  # 'openai' | 'anthropic'; required for external
    # The endpoint's own cache lifetime, as the user knows it. Stored only: it is
    # never sent, and nothing schedules against it yet (a native warm idle fold
    # is follow-up work). Bounded at 24h, the longest retention offered today.
    cache_warm_seconds: int | None = Field(default=None, ge=1, le=86_400)
    agent_use_restriction: Literal["allowed", "interactive_only"] | None = None
    # Ids to catalog when the endpoint does not list its models (Alibaba's plans).
    model_ids: list[str] | None = None
    api_key: str | None = None  # optional admin token (--api-key)


class UpdateExternalServerBody(BaseModel):
    base_url: str
    dialect: str = "openai"
    api_key: str | None = None
    # Declarative only: this remains endpoint metadata and does not schedule a
    # cache warm or alter managed-server cache behavior.
    cache_warm_seconds: int | None = Field(default=None, ge=1, le=86_400)
    agent_use_restriction: Literal["allowed", "interactive_only"] | None = None
    # None keeps the stored ids; a list (even empty) replaces them.
    model_ids: list[str] | None = None


@router.put("/{server_id}")
async def update_server(
    server_id: str, body: UpdateExternalServerBody, user: dict = Depends(require_user),
):
    """Update a user-owned External endpoint after validating its new settings."""
    if (body.api_key or "").strip():
        require_person_for_key(user, kind="server", name=server_id, action="write")
    result = await update_external_server(server_id, body)
    if (body.api_key or "").strip():
        audit_key_write(user, kind="server", name=server_id, action="write")
    return result


@router.post("")
async def add_server(body: AddServerBody, user: dict = Depends(require_user)):
    """Manually register an inference server.

    A ``base_url`` means a user-owned cloud endpoint and takes the External path;
    without one this stays exactly the oMLX host+port flow it has always been.
    """
    if (body.api_key or "").strip():
        require_person_for_key(user, kind="server", name=body.name, action="write")
    if (body.base_url or "").strip():
        result = await add_external_server(body)
        if (body.api_key or "").strip():
            audit_key_write(user, kind="server", name=result["server_id"], action="write")
        return result
    if not body.hostname.strip():
        # Both paths need something to reach. Reporting "host is required" when
        # the user pasted a full URL would send them in circles.
        raise HTTPException(400, detail="Provide a hostname or a full base_url")
    result = await _add_omlx_server(body)
    if (body.api_key or "").strip():
        audit_key_write(user, kind="server", name=result["server_id"], action="write")
    return result


@router.get("/{server_id}")
async def server_detail(server_id: str):
    server = await run_in_db(nd.get_server, server_id)
    if not server:
        raise HTTPException(404, detail="Server not found")

    cfg = {"server_id": server_id}
    pv = for_provider(server["provider"])
    loaded = await pv.list_loaded(cfg)
    disk = await pv.list_disk_models(cfg)

    network = await run_in_db(_network_view, server_id)
    return {
        **_public_server_view(server),
        **network,
        **(await run_in_db(billing_views, [server_id]))[server_id],
        "loaded_models": loaded,
        "available_models": disk,
    }


class ModelBody(BaseModel):
    model: str


def _refuse_external(server: dict) -> None:
    """External models are never loaded: the provider runs them. Offering one
    is its catalog row's enabled flag, set through PATCH /api/settings/models."""
    if server.get("kind") == "external":
        raise HTTPException(
            400, detail="External models are turned on or off in the model catalog",
        )


@router.post("/{server_id}/models/load")
async def load_model(server_id: str, body: ModelBody):
    server = await run_in_db(nd.get_server, server_id)
    if not server:
        raise HTTPException(404, detail="Server not found")
    _refuse_external(server)

    cfg = {"server_id": server_id}
    pv = for_provider(server["provider"])
    result = await pv.load_model(cfg, body.model)

    if not result.get("ok"):
        upstream = result.get("status")
        status = upstream if isinstance(upstream, int) and 400 <= upstream < 600 else 500
        emit_product_event(
            "model", "load.failed",
            properties={
                "provider": server["provider"], "model_id": body.model,
                "server_id": server_id, "status": status,
                "error": (result.get("message") or "Load failed")[:200],
            },
        )
        raise HTTPException(status, detail=result.get("message", "Load failed"))

    notify_cached_model_loaded(server_id, body.model)
    import asyncio
    asyncio.create_task(nd.discover_and_notify(server_id))

    emit_product_event(
        "model", "load.completed",
        properties={
            "provider": server["provider"], "model_id": body.model,
            "server_id": server_id,
        },
    )

    return result


@router.post("/{server_id}/models/unload")
async def unload_model(server_id: str, body: ModelBody):
    server = await run_in_db(nd.get_server, server_id)
    if not server:
        raise HTTPException(404, detail="Server not found")
    _refuse_external(server)

    cfg = {"server_id": server_id}
    pv = for_provider(server["provider"])
    result = await pv.unload_model(cfg, body.model)

    if not result.get("ok"):
        raise HTTPException(500, detail=result.get("message", "Unload failed"))

    notify_cached_model_unloaded(server_id, body.model)
    emit_product_event(
        "model", "unloaded",
        properties={
            "provider": server["provider"], "model_id": body.model,
            "server_id": server_id,
        },
    )
    import asyncio
    asyncio.create_task(nd.discover_and_notify(server_id))
    return result


@router.delete("/{server_id}")
async def remove_server(server_id: str):
    removed = await run_in_db(nd.remove_server, server_id)
    if not removed:
        raise HTTPException(404, detail="Server not found")
    return {"ok": True, "server_id": server_id}


class EnabledBody(BaseModel):
    enabled: bool


class PrivateBody(BaseModel):
    private: StrictBool


@router.put("/{server_id}/private")
async def set_server_private(
    server_id: str, body: PrivateBody, user: dict = Depends(require_user),
):
    """Set the person's private-network mark without replacing server metadata."""
    require_person_for_key(user, kind="server", name=server_id, action="write")
    server = await run_in_db(nd.get_server, server_id)
    if not server:
        raise HTTPException(404, detail="Server not found")
    await run_in_db(nd.set_server_metadata, server_id, private=body.private)
    return {"server_id": server_id, "private": body.private}


@router.put("/{server_id}/enabled")
async def set_server_enabled(server_id: str, body: EnabledBody):
    """Turn a server off (its models leave the selector) or back on."""
    if not await run_in_db(nd.set_server_enabled, server_id, body.enabled):
        raise HTTPException(404, detail="Server not found")
    if body.enabled:
        import httpx

        # Probe now rather than at the next sweep; back online, it rediscovers.
        server = await run_in_db(nd.get_server, server_id)
        async with httpx.AsyncClient(timeout=5.0) as client:
            await nd._sweep_one(client, server)
    else:
        nd._notify_models_changed_safe()
    server = await run_in_db(nd.get_server, server_id)
    return {"ok": True, "server_id": server_id, "status": server["status"]}


@router.post("/{server_id}/test")
async def test_server(server_id: str):
    """Probe an External endpoint now and rediscover its models."""
    from api.external_connection_check import check_external_server

    server = await run_in_db(nd.get_server, server_id)
    if not server or server.get("status") == "removed":
        raise HTTPException(404, detail="Server not found")
    if server.get("kind") != "external":
        raise HTTPException(400, detail="Only an External endpoint is tested here")
    return await check_external_server(server_id, server)


class ImageInputBody(BaseModel):
    mode: Literal["auto", "yes", "no"]


@router.put("/{server_id}/models/{model_id:path}/image-input")
async def set_model_image_input(server_id: str, model_id: str, body: ImageInputBody):
    """Pin an External model's image support, or (``auto``) detect it again now."""
    from llm.external_image_input import set_image_input

    server = await run_in_db(nd.get_server, server_id)
    if not server or server.get("kind") != "external":
        raise HTTPException(404, detail="External endpoint not found")
    if not await set_image_input(server_id, model_id, body.mode):
        raise HTTPException(404, detail="Model not found on this endpoint")
    notify_models_changed()
    return {"ok": True}


@router.get("/{server_id}/models")
async def list_models(server_id: str):
    """Return daemon models as ``{loaded, available}``."""
    server = await run_in_db(nd.get_server, server_id)
    if not server:
        raise HTTPException(404, detail="Server not found")
    return await list_daemon_models(server_id, server)
