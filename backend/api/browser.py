"""Authenticated browser-pane controls bound to an existing CORSIA thread."""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from infra.auth import authenticate
from infra.database import get_db
from infra.queries import get_thread_for_user
from host_access import browser_control, browser_pane
from host_access.browser_session_state import BrowserSessionError, BrowserSetupError
from infra.product_events import emit_product_event

router = APIRouter(prefix="/api/browser", tags=["browser"])


def _owned_thread(thread_id: str, authorization: str = "") -> dict:
    user = authenticate(authorization.removeprefix("Bearer "))
    if not user:
        raise HTTPException(401)
    db = get_db()
    try:
        if not get_thread_for_user(thread_id, user["id"], db=db):
            raise HTTPException(404)
    finally:
        db.close()
    return user


def _http_url(url: str) -> str:
    if not browser_control.is_http_url(url):
        raise HTTPException(422, "Browser URL must be http or https")
    return url


@router.post("/{thread_id}/open")
async def open_url(
    thread_id: str,
    url: str,
    authorization: str = Header(default=""),
):
    """Start the owned thread's browser at an HTTP(S) URL; an open browser navigates by stream."""
    user = _owned_thread(thread_id, authorization)
    try:
        result = await browser_pane.open_url(thread_id, _http_url(url))
    except (BrowserSessionError, BrowserSetupError) as exc:
        raise HTTPException(409, str(exc)) from exc
    if result.startswith("Error:"):
        raise HTTPException(409, result)
    emit_product_event("browser_pane", "opened", thread_id=thread_id, user_id=user["id"])
    return {"opened": True}
