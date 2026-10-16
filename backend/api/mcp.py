"""Bounded server-side MCP discovery and import routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from api.connected_apps import connected_apps_router
from infra.auth import require_user_header_only
from infra.db_async import run_off_db

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
async def import_servers(_body: dict[str, Any], _user: dict = Depends(require_user_header_only)) -> dict[str, Any]:
    """Refuse the retired settings-backed import path until it creates connected apps."""
    raise HTTPException(410, LEGACY_IMPORT_REMEDY)


router.include_router(connected_apps_router)
