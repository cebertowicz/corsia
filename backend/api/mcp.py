"""Bounded server-side MCP discovery and import routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from api.connected_apps import connected_apps_router
from infra.auth import require_user_header_only
from infra.db_async import run_in_db, run_off_db

router = APIRouter(prefix="/api/mcp", tags=["mcp"])
LEGACY_IMPORT_REMEDY = (
    "Bringing apps over from other apps isn't available in this build yet; add the app in Connected apps."
)


@router.get("/sources")
async def get_sources(_user: dict = Depends(require_user_header_only)) -> dict[str, Any]:
    """List only detected MCP server names, kinds and source apps."""
    from tools.mcp_import import detected_sources

    return {"sources": await run_off_db(detected_sources)}


@router.post("/import")
async def import_servers(body: dict[str, Any], user: dict = Depends(require_user_header_only)) -> dict[str, Any]:
    """Import locally reread source/name pairs as approval-gated Connected Apps."""
    if set(body) != {"servers"} or not isinstance(body["servers"], list):
        raise HTTPException(422, "select apps with a servers list of source/name pairs")
    selected = body["servers"]
    if any(not isinstance(item, dict) or set(item) != {"source", "name"}
           or not all(isinstance(item.get(key), str) and item[key] for key in ("source", "name"))
           for item in selected):
        raise HTTPException(422, "each selected app must contain only source and name")
    from infra.database import get_db
    from tools.mcp_import import import_selected

    actor = user.get("thread_id") or user.get("id") or "unknown"
    return await run_in_db(lambda: import_selected(selected, db=get_db(), actor=str(actor)))


router.include_router(connected_apps_router)
