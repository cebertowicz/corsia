"""Test connection for one External endpoint: probe it now, then rediscover.

The answer is what the endpoint says at the moment of the test, never the
status the last sweep stored.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import HTTPException

from api.network_server_helpers import _probe_external
from infra.db_async import run_in_db
from llm import network_discovery as nd
from llm.external_declared_models import declared_model_ids
from llm.external_image_input import detect_image_input, image_input_summary, mark_for_recheck

log = logging.getLogger(__name__)


async def check_external_server(server_id: str, server: dict) -> dict:
    """``{"ok", "message"}`` naming what the endpoint answered and what was found."""
    from infra.key_store_servers import resolve_server
    from llm.external_provider import auth_headers

    if server.get("status") == "disabled":
        return {"ok": False, "message": "This endpoint is switched off. Switch it on to test it."}
    stored = await run_in_db(nd.read_server_metadata, server_id)
    if stored is None:
        return {"ok": False, "message": "This endpoint's saved settings could not be read."}
    dialect = server.get("dialect") or "openai"
    base = stored.get("base_url")
    if not base:
        return {"ok": False, "message": "This endpoint has no saved URL. Set it under Advanced."}
    try:
        await _probe_external(
            base, dialect, auth_headers(dialect, await run_in_db(resolve_server, server_id)), action="test",
            declared=declared_model_ids(stored),
        )
    except HTTPException as e:
        return {"ok": False, "message": str(e.detail)}

    # Reachable: record it now and rediscover, rather than waiting for a sweep.
    # A test re-checks image support too; rediscovery probes the marked rows.
    try:
        await run_in_db(mark_for_recheck, server_id)
    except Exception:
        log.warning("external image recheck could not be marked for %s", server_id)
    async with httpx.AsyncClient(timeout=5.0) as client:
        await nd._sweep_one(client, server)
    discovery = await nd.discover_and_notify(server_id)
    if not discovery.get("ok"):
        error = discovery.get("error") or "No completed model discovery result"
        return {"ok": False, "message": f"Reached, but model discovery failed: {error}"}
    # Optional image enrichment must not hide the core check's actual receipt.
    images = ""
    try:
        if await detect_image_input(server_id):
            nd._notify_models_changed_safe()
        images = await run_in_db(image_input_summary, server_id)
    except Exception:
        log.warning("external image support could not be checked for %s", server_id)
    count = int(discovery.get("discovered") or 0) + int(discovery.get("updated") or 0)
    found = f"Reachable. {count} model{'' if count == 1 else 's'} found."
    return {"ok": True, "message": f"{found} {images}".strip()}
