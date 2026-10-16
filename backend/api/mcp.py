"""Bounded server-side MCP discovery and import routes."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from api.connected_apps import connected_apps_router
from infra.auth import require_user_header_only
from infra.db_async import run_in_db, run_off_db

router = APIRouter(prefix="/api/mcp", tags=["mcp"])
log = logging.getLogger(__name__)


@router.get("/sources")
async def get_sources(_user: dict = Depends(require_user_header_only)) -> dict[str, Any]:
    """List only detected MCP server names, kinds and source apps."""
    from tools.mcp_import import detected_sources

    return {"sources": await run_off_db(detected_sources)}


@router.post("/import")
async def import_servers(body: dict[str, Any], _user: dict = Depends(require_user_header_only)) -> dict[str, Any]:
    """Import chosen server configs and reload MCP sessions without a restart."""
    selected = body.get("servers")
    if not isinstance(selected, list) or not selected or any(
        not isinstance(item, dict) or set(item) != {"source", "name"}
        or not all(isinstance(value, str) and value for value in item.values())
        for item in selected
    ):
        raise HTTPException(422, "servers must be nonempty source/name pairs returned by GET /api/mcp/sources")
    from tools.mcp_import import prepare_import, save_import

    additions, statuses = await run_off_db(prepare_import, selected)
    if additions:
        outcomes = await run_in_db(save_import, additions)
        for status in statuses:
            if status["status"] == "pending" and outcomes.get(status["name"]) == "conflict":
                status["status"] = (
                    f"failed: a server named {status['name']} is already set up in CORSIA; "
                    "kept the existing one"
                )
    errors: dict[str, str] = {}
    if any(status["status"] == "pending" for status in statuses):
        from infra.settings import get_mcp_servers
        from tools.mcp_runtime import reload_mcp_runtime

        try:
            errors = await reload_mcp_runtime(await run_in_db(get_mcp_servers))
        except Exception:
            log.exception("MCP reload after import failed")
            errors = {status["name"]: "MCP reload could not complete; retry this import once"
                      for status in statuses if status["status"] == "pending"}
    for status in statuses:
        if status["status"] == "pending":
            status["status"] = f"failed: {errors[status['name']]}" if status["name"] in errors else "working"
    return {"servers": statuses}


router.include_router(connected_apps_router)
