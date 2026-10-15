"""The platform-held Google sign-in routes.

``connect`` and ``status`` answer any signed-in caller and never carry a
token. The callback is Google's redirect into the person's own browser: it has
no session to check, so it is loopback-only and trusts nothing but the
single-use state it issued (``connections.google.oauth``).
"""

from __future__ import annotations

import html
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from connections.google import oauth
from connections.google.client import GoogleError
from infra.auth import require_user
from infra.db_async import run_in_db
from infra.local_connection import is_direct_loopback

router = APIRouter(prefix="/api/google", tags=["google"])
log = logging.getLogger(__name__)

MAX_BODY_BYTES = 2048
CONNECTED_PAGE = "Google is connected. You can close this tab and go back to CORSIA."


def _page(message: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><title>CORSIA</title><p>{html.escape(message)}</p>", status_code=status,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.post("/connect")
async def connect(request: Request, _user: dict = Depends(require_user)) -> dict[str, Any]:
    """Start a sign-in; returns the URL for the person to open. Accepts ``{"client_file": path}``."""
    try:
        raw = await request.body()
        body = json.loads(raw) if raw and len(raw) <= MAX_BODY_BYTES else {}
        client_file = body.get("client_file") if isinstance(body, dict) else None
    except ValueError:
        client_file = None
    try:
        url = await oauth.start_sign_in(client_file)
    except GoogleError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"url": url}


@router.get("/oauth/callback")
async def callback(request: Request, code: str | None = None, state: str | None = None) -> HTMLResponse:
    if not is_direct_loopback(request):
        raise HTTPException(403, "The Google sign-in callback is loopback-only")
    try:
        await oauth.complete_sign_in(code, state)
    except GoogleError as exc:
        return _page(str(exc), 400)
    except Exception:
        log.exception("Google sign-in callback failed")
        return _page("Google sign-in failed. Go back to CORSIA and try again.", 500)
    return _page(CONNECTED_PAGE)


@router.get("/status")
async def status(_user: dict = Depends(require_user)) -> dict[str, Any]:
    return await run_in_db(oauth.account_status)
