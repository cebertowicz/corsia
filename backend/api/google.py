"""The platform-held Google sign-in routes, one account per sign-in.

``connect`` and ``status`` answer any signed-in caller and never carry a
token. An account the person adds here works in all their projects; one an
agent's token adds works only where that agent works. The callback is Google's redirect into the person's own browser: it has
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

from api.connected_app_access import audit_actor
from connections.app_reach import AppReach
from connections.google import oauth
from connections.google.client import GoogleError
from infra.auth import is_person_session, require_user
from infra.db_async import run_in_db
from infra.local_connection import is_direct_loopback

router = APIRouter(prefix="/api/google", tags=["google"])
log = logging.getLogger(__name__)

MAX_BODY_BYTES = 2048
CONNECTED_PAGE = "Google is connected. You can close this tab and go back to CORSIA."
CALLBACK_FAILED = "Google sign-in couldn't finish. Go back to CORSIA and try again."


def _callback_message(error: GoogleError) -> str:
    """The error's browser copy when it carries one; agent-facing text never reaches the browser."""
    if isinstance(error, oauth.GoogleSignInError) and error.browser_message:
        return error.browser_message
    return CALLBACK_FAILED


def _page(message: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><title>CORSIA</title><p>{html.escape(message)}</p>", status_code=status,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


ALL_PROJECTS = AppReach(mode="all", project_ids=frozenset(), outside_projects=False)


def _body_field(body: object, key: str) -> str | None:
    value = body.get(key) if isinstance(body, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _agent_thread_id(user: dict, missing_thread_message: str) -> str:
    """The calling agent's own server-known thread; which accounts it may see is ``account_reaches_thread``'s."""
    from api.connected_apps import _agent_thread
    from infra.database import get_db

    if not isinstance(user.get("thread_id"), str) or not user["thread_id"]:
        raise HTTPException(403, missing_thread_message)
    db = get_db()
    try:
        thread_id, _scope = _agent_thread(user, db)
        return thread_id
    finally:
        db.close()


def _visible(account_id: str, thread_id: str) -> bool:
    from connections.google.accounts import visible_google_account
    from infra.database import get_db

    db = get_db()
    try:
        return visible_google_account(account_id, thread_id, db=db) is not None
    finally:
        db.close()


@router.post("/connect")
async def connect(request: Request, user: dict = Depends(require_user)) -> dict[str, Any]:
    """Start one sign-in; returns the URL for the person to open and the account it is for.

    Accepts ``{"client_file": path, "account_id": id}``: with ``account_id`` it signs that
    account in again, otherwise it adds one.
    """
    try:
        raw = await request.body()
        body = json.loads(raw) if raw and len(raw) <= MAX_BODY_BYTES else {}
    except ValueError:
        body = {}
    account_id = _body_field(body, "account_id")
    person = is_person_session(user)
    if account_id is not None and not person:
        thread_id = await run_in_db(
            _agent_thread_id, user,
            "Google account sign-in from an agent needs its current thread context; call this through corsia_api from that thread.",
        )
        if not await run_in_db(_visible, account_id, thread_id):
            raise HTTPException(400, oauth.UNKNOWN_ACCOUNT)  # Exactly what an unknown id gets below.
    adding_thread_id = None
    if account_id is None and not person:
        adding_thread_id = await run_in_db(
            _agent_thread_id, user,
            "Google account add from an agent needs its current thread context; call this through corsia_api from that thread.",
        )
    try:
        url, signing_in = await oauth.start_sign_in(
            _body_field(body, "client_file"), actor=audit_actor(user), account_id=account_id,  # type: ignore[arg-type]
            adding_thread_id=adding_thread_id,
            reach=ALL_PROJECTS if person and account_id is None else None,
        )
    except GoogleError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"url": url, "account_id": signing_in}


@router.get("/oauth/callback")
async def callback(request: Request, code: str | None = None, state: str | None = None) -> HTMLResponse:
    if not is_direct_loopback(request):
        raise HTTPException(403, "The Google sign-in callback is loopback-only")
    try:
        await oauth.complete_sign_in(code, state)
    except GoogleError as exc:
        return _page(_callback_message(exc), 400)
    except Exception:
        log.exception("Google sign-in callback failed")
        return _page("Google sign-in failed. Go back to CORSIA and try again.", 500)
    return _page(CONNECTED_PAGE)


@router.get("/status")
async def status(user: dict = Depends(require_user)) -> dict[str, Any]:
    if is_person_session(user):
        return await run_in_db(oauth.accounts_status)

    thread_id = await run_in_db(
        _agent_thread_id, user,
        "Google account status from an agent needs its current thread context; call this through corsia_api from that thread.",
    )
    return await run_in_db(oauth.accounts_status, thread_id)
