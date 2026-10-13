"""Stored model-source facts, plus one bounded manual recheck.

A completed recheck returns the same source shape as GET. An incomplete one
fails the request instead of presenting prior rows as fresh check results.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from connections import harness_usage
from connections.models import get_connection
from connections.source_state import HARNESS_PROVIDERS, check_harness_source, row_state
from infra.auth import require_user, require_user_header_only
from infra.db_async import run_in_db, run_off_db
from llm import local_sources, network_discovery
from llm.local_sources import CHECK_AGAIN, external_sources, omlx_source

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/providers", tags=["providers"])
# The payload names the app as the fix, never a terminal command; the terminal
# summary composes its own sentence from the state (``cli_sources``).
APP_FIX = "Connect it in the app."
_HARNESS_SOURCES = {
    "claude-code": ("Claude Code", APP_FIX, APP_FIX),
    "codex": ("Codex", APP_FIX, APP_FIX),
}
_inflight: asyncio.Task | None = None
_INCOMPLETE = "Model source checks could not be completed. Check again."


class SourceChecksIncomplete(RuntimeError):
    """A recheck did not complete; its message is safe to show a user or an agent."""


def _default_limit(provider: str) -> dict[str, Any] | None:
    """A source row is the provider's own sign-in: its Default account's limit."""
    from connections.vendor_accounts import resolve_account_id
    from infra.database import get_db

    db = get_db()
    try:
        identity = resolve_account_id(provider, db=db)
        return harness_usage.active_limit(provider=provider, account_id=identity, db=db) if identity else None
    finally:
        db.close()


def _harness_source(provider: str) -> dict[str, Any]:
    name, install, login = _HARNESS_SOURCES[provider]
    row = get_connection(provider)
    view = row_state(row)
    fix = {"not_installed": install, "needs_auth": login,
           "unverified": CHECK_AGAIN}.get(view["state"])
    from connections.conductor_account import source_account_view

    # The first-run card's account lines (the picker's account rows for this vendor)
    # and the conductor's binding when its account was removed or turned off.
    accounts, lost_binding = source_account_view(provider)
    source = {
        "source": provider, "name": name, **view, "fix": fix,
        "enabled": bool((row or {}).get("enabled", 1)),
        "accounts": accounts, "lost_binding": lost_binding,
    }
    if view["state"] == "not_installed":
        from api.provider_cli_link import invisible_binary
        source["invisible_binary"] = invisible_binary(provider)
    if view["state"] == "ready":
        limit = _default_limit(provider)
        if limit:
            source.update(limited=True, resets_at=limit.get("resets_at"))
    return source


def list_sources() -> list[dict[str, Any]]:
    """Claude Code, Codex, local model, then enabled External endpoints."""
    membership = local_sources.catalog_membership()
    return [*(_harness_source(provider) for provider in HARNESS_PROVIDERS),
            omlx_source(membership), *external_sources(membership)]


async def _recheck() -> None:
    """Run existing checks and settle every child before reporting completion.

    External endpoints take the same probe, sweep and discovery as their Test
    button, and GitHub the same refresh as startup. A failed receipt is a
    request failure, not another source store.
    """
    from api.external_connection_check import check_external_server
    from connections import refresh_github_connection

    servers = await run_in_db(network_discovery.get_servers)
    live = [s for s in servers if s.get("status") != "disabled"]

    async def sweep_omlx() -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            for server in (s for s in live if s.get("kind") != "external"):
                await network_discovery._sweep_one(client, server)

    async def refresh_github() -> dict[str, bool]:
        return {"ok": await run_off_db(refresh_github_connection, keep_row_on_failed_probe=True)}

    results = await asyncio.gather(
        *(check_harness_source(provider, rediscover=True) for provider in HARNESS_PROVIDERS),
        sweep_omlx(),
        refresh_github(),
        *(check_external_server(s["id"], s) for s in live if s.get("kind") == "external"),
        return_exceptions=True,
    )
    if any(isinstance(r, BaseException) or isinstance(r, dict) and not r.get("ok", False)
           for r in results):
        raise RuntimeError("Model source checks incomplete")
    from api.provider_cli_link import cache_invisible_binary

    for provider in HARNESS_PROVIDERS:
        row = await run_in_db(get_connection, provider)
        if row_state(row)["state"] == "not_installed":
            await asyncio.to_thread(cache_invisible_binary, provider)


@router.get("/sources")
async def get_sources(_user: dict = Depends(require_user)) -> dict[str, Any]:
    return {"sources": await run_in_db(list_sources)}


class HarnessEnabledBody(BaseModel):
    enabled: bool


@router.put("/harnesses/{provider}/enabled")
async def set_harness_enabled(
    provider: str, body: HarnessEnabledBody, _user: dict = Depends(require_user),
) -> dict[str, Any]:
    """Persist a CLI harness switch without modifying observed source state."""
    if provider not in HARNESS_PROVIDERS:
        raise HTTPException(404, f"Unknown harness provider: {provider}")
    from connections.models import set_harness_enabled as persist_harness_enabled
    from infra.product_events import emit_product_event
    from infra.state.ws import notify_models_changed

    row = await run_in_db(persist_harness_enabled, provider, body.enabled)
    if row is None:
        raise HTTPException(404, f"Harness provider is not configured: {provider}")
    emit_product_event(
        "provider", "harness_enabled.changed",
        properties={"provider": provider, "enabled": body.enabled},
    )
    notify_models_changed()
    if body.enabled:
        from ws.ingress import track_background_task

        track_background_task(asyncio.create_task(check_harness_source(provider)))
    return {"ok": True, "provider": provider, "enabled": body.enabled}


@router.post("/local/setup")
async def setup_local_source(
    no_subscription_only: bool = Body(False, embed=True),
    automatic: bool = Body(False, embed=True),
    profile: str | None = Body(None, embed=True),
    _user: dict = Depends(require_user_header_only),
) -> dict[str, Any]:
    """Start the local setup flight, or report the one already running.

    ``profile`` names a local model profile (``recommended_profile`` on GET); omitted, the default.
    """
    from llm.local_setup import begin_local_setup
    from llm.local_setup_gates import ProfileRefused

    try:
        return await begin_local_setup(
            no_subscription_only=no_subscription_only, automatic=automatic, profile_id=profile,
        )
    except ProfileRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/local/starter")
async def add_starter_model(_user: dict = Depends(require_user_header_only)) -> dict[str, Any]:
    """Download the default local model into the person's own oMLX, writing none of its settings."""
    from llm.local_setup import begin_add_starter_model

    return await begin_add_starter_model()


@router.get("/local/setup")
async def get_local_setup(_user: dict = Depends(require_user)) -> dict[str, Any]:
    """The local setup flight record, whose oMLX this is, and the model this Mac suits.

    ``omlx_is_persons_own`` is true when the oMLX here is one CORSIA did not install;
    setup adds no models to it, so ``recommended_profile`` is then null.
    """
    from llm.local_setup import setup_status
    from llm.local_setup_gates import host_ram_bytes, recommended_profile
    from llm.local_setup_ownership import existing_install_is_foreign

    foreign = await asyncio.to_thread(existing_install_is_foreign)
    ram_bytes = await asyncio.to_thread(host_ram_bytes)
    return {**setup_status(), "omlx_is_persons_own": foreign,
            "recommended_profile": recommended_profile(ram_bytes, foreign)}


async def recheck_sources_once() -> list[dict[str, Any]]:
    """Run the checks once and return ``list_sources()``.

    Concurrent callers (the route and the ``model_sources`` tool) share one
    run. An incomplete run raises ``SourceChecksIncomplete``, whose only text
    is the safe sentence; it never returns stored rows as fresh results.
    """
    global _inflight
    if _inflight is None or _inflight.done():
        _inflight = asyncio.create_task(_recheck())
    try:
        await asyncio.shield(_inflight)
        return await run_in_db(list_sources)
    except Exception:
        log.warning("one or more model source checks could not be completed")
        raise SourceChecksIncomplete(_INCOMPLETE) from None


@router.post("/sources/omlx/settings/apply")
async def apply_foreign_omlx_settings(
    body: dict = Body(...), _user: dict = Depends(require_user_header_only),
) -> dict[str, Any]:
    """Apply precisely the foreign-oMLX diff the conductor just showed."""
    from llm.local_setup_ownership import existing_install_is_foreign
    from llm.omlx_global_settings import WRITABLE_GLOBAL_SETTINGS, differences, fingerprint, record_observation, reports
    from llm.providers.omlx_settings import save_global_settings

    supplied = body.get("fingerprint") if isinstance(body, dict) and set(body) == {"fingerprint"} else None
    if not isinstance(supplied, str) or not supplied:
        raise HTTPException(400, "A shown oMLX settings fingerprint is required")
    if not await asyncio.to_thread(existing_install_is_foreign):
        raise HTTPException(409, "oMLX settings cannot be changed through this route")
    servers = [server for server in await run_in_db(network_discovery.get_servers)
               if server.get("provider") == "omlx" and server.get("kind") != "external"
               and server.get("status") == "online"]
    if len(servers) != 1:
        raise HTTPException(409, "Exactly one reachable foreign oMLX is required")
    server = servers[0]
    entries = differences([server["id"]])
    if not entries:
        raise HTTPException(409, "No shown oMLX settings are available to apply")
    if any(row["setting"] not in WRITABLE_GLOBAL_SETTINGS for row in entries):
        raise HTTPException(409, "The settings changed since you were shown them; show the new diff and ask again")
    if supplied != fingerprint(server["id"], entries):
        raise HTTPException(409, "the settings changed since you were shown them; show the new diff and ask again")
    pairs = {row["setting"]: row["recommended"] for row in entries}
    saved = await save_global_settings({"server_id": server["id"]}, pairs)
    if not saved.get("ok"):
        raise HTTPException(int(saved.get("status", 503)), str(saved.get("message")))
    verified = await record_observation(server, retain_on_failure=True)
    remaining = (differences([server["id"]]) if verified else entries) or []
    remaining_names = {row["setting"] for row in remaining}
    results = []
    for row in entries:
        done = verified and row["setting"] not in remaining_names and reports(server["id"], row["setting"])
        results.append({"setting": row["setting"], "saved": done,
                        "active": done and row["setting"] in saved.get("runtime_applied", [])})
    return {"results": results, "verified": verified,
            "omlx_settings": {"differences": remaining, "fingerprint": fingerprint(server["id"], remaining)}}


@router.post("/sources/recheck")
async def recheck_sources(_user: dict = Depends(require_user_header_only)) -> dict[str, Any]:
    """Concurrent callers share one run; an incomplete result uses a safe HTTP error."""
    try:
        return {"sources": await recheck_sources_once()}
    except SourceChecksIncomplete as incomplete:
        raise HTTPException(503, str(incomplete)) from None
