"""Stored model-source facts, plus one bounded manual recheck.

A completed recheck returns the same source shape as GET. An incomplete one
fails the request instead of presenting prior rows as fresh check results.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException

from connections import harness_usage
from connections.models import get_connection
from connections.source_state import HARNESS_PROVIDERS, check_harness_source, row_state
from infra.auth import require_user, require_user_header_only
from infra.db_async import run_in_db, run_off_db
from llm import local_sources, network_discovery
from llm.local_setup_foreign import apply_foreign_omlx_choice
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
    view = row_state(get_connection(provider))
    fix = {"not_installed": install, "needs_auth": login,
           "unverified": CHECK_AGAIN}.get(view["state"])
    from connections.corsia_agent_account import source_account_view

    # The first-run card's account lines (the picker's account rows for this vendor)
    # and the conductor's binding when its account was removed or turned off.
    accounts, lost_binding = source_account_view(provider)
    source = {
        "source": provider, "name": name, **view, "fix": fix,
        "accounts": accounts, "lost_binding": lost_binding,
    }
    if view["state"] == "not_installed":
        from api.provider_cli_link import invisible_binary
        source["invisible_binary"] = invisible_binary(provider)
    if view["state"] == "unverified":
        from cli_location import package_manager_for

        manager = package_manager_for(provider)
        if manager:
            source["cli_package_manager"] = manager
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


@router.post("/local/setup")
async def setup_local_source(
    no_subscription_only: bool = Body(False, embed=True),
    automatic: bool = Body(False, embed=True),
    onboarding: bool = Body(False, embed=True),
    other_omlx: Literal["stop", "keep"] | None = Body(None, embed=True),
    _user: dict = Depends(require_user_header_only),
) -> dict[str, Any]:
    """Start managed setup after an explicit choice about an identified foreign process."""
    if refusal := await asyncio.to_thread(
        apply_foreign_omlx_choice,
        other_omlx=other_omlx,
    ):
        raise HTTPException(status_code=409, detail={"code": refusal})
    from llm.local_setup_new_omlx import begin_local_setup

    return await begin_local_setup(no_subscription_only=no_subscription_only, automatic=automatic, onboarding=onboarding)



@router.post("/local/cancel")
async def cancel_local_source(_user: dict = Depends(require_user_header_only)) -> dict[str, Any]:
    """Cancel a running local-model download; a completed flight is a no-op."""
    from llm.local_setup import cancel_local_setup

    return await cancel_local_setup()


@router.get("/local/setup")
async def get_local_setup(_user: dict = Depends(require_user)) -> dict[str, Any]:
    """The managed local setup flight and CORSIA's current model pick."""
    from llm.local_setup import setup_status
    from llm.local_setup_prepare import pick_view

    return {**setup_status(), "local_model": await pick_view()}


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



@router.post("/sources/recheck")
async def recheck_sources(_user: dict = Depends(require_user_header_only)) -> dict[str, Any]:
    """Concurrent callers share one run; an incomplete result uses a safe HTTP error."""
    try:
        return {"sources": await recheck_sources_once()}
    except SourceChecksIncomplete as incomplete:
        raise HTTPException(503, str(incomplete)) from None
