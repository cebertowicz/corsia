"""Authenticated browser-pane controls bound to an existing CORSIA thread."""
from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Header, HTTPException

from infra.auth import authenticate
from infra.database import get_db
from infra.queries import get_thread_for_user
from host_access import browser_pane
from host_access.browser_session_state import BrowserSessionError, BrowserSetupError

router = APIRouter(prefix="/api/browser", tags=["browser"])


def _owned_thread(thread_id: str, authorization: str = "") -> None:
    user = authenticate(authorization.removeprefix("Bearer "))
    if not user:
        raise HTTPException(401)
    db = get_db()
    try:
        if not get_thread_for_user(thread_id, user["id"], db=db):
            raise HTTPException(404)
    finally:
        db.close()


def _http_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(422, "Browser URL must be http or https")
    return url


@router.post("/{thread_id}/control")
async def set_control(
    thread_id: str,
    has_control: bool,
    authorization: str = Header(default=""),
):
    """Transfer the owned thread's browser input authority to or from its user."""
    _owned_thread(thread_id, authorization)
    browser_pane.set_user_control(thread_id, has_control)
    return {"has_control": has_control}


@router.post("/{thread_id}/open")
async def open_url(
    thread_id: str,
    url: str,
    authorization: str = Header(default=""),
):
    """Navigate the owned thread's browser to an HTTP(S) URL."""
    _owned_thread(thread_id, authorization)
    try:
        result = await browser_pane.open_url(thread_id, _http_url(url))
    except (BrowserSessionError, BrowserSetupError) as exc:
        raise HTTPException(409, str(exc)) from exc
    if result.startswith("Error:"):
        raise HTTPException(409, result)
    return {"opened": True}
