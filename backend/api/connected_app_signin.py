"""Sign in to, and out of, a hosted connected app. Person only: an agent is refused and recorded."""
from __future__ import annotations

import ipaddress
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from infra.auth import is_person_session, require_user, token_from_request
from infra.db_async import run_in_db

signin_router = APIRouter()
_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "[::1]", "::1"}
_NOT_PERSON = "Only the signed-in person can sign in to an app or sign out of it; open Connected apps."
_NOT_HERE = "You can only sign in on the Mac CORSIA runs on. Open CORSIA there and press Sign in."


def _db():
    from infra.database import get_db
    return get_db()


def sign_in_here(request: Request) -> bool:
    """Whether this browser is on the Mac running CORSIA, so the loopback callback can reach the backend."""
    host = request.headers.get("host") or ""
    name = host[: host.index("]") + 1] if host.startswith("[") else host.rsplit(":", 1)[0]
    client = request.client.host if request.client else ""
    try:
        loopback_client = ipaddress.ip_address(client).is_loopback
    except ValueError:
        loopback_client = False
    return loopback_client and name.lower() in _LOOPBACK_NAMES and not request.headers.get("x-forwarded-for")


def _person_only(user: dict, action: str, app_id: str) -> tuple[str, str]:
    from api.connected_apps import _audit_actor, _refusal

    actor = _audit_actor(user)
    if not is_person_session(user):
        from connections.app_lifecycle_support import record_refusal

        record_refusal(app_id, f"refused_{action}", actor=actor, db=_db())
        raise _refusal(_NOT_PERSON, 403)
    return actor


def _load(app_id: str) -> tuple[str, dict[str, Any], str]:
    from api.connected_apps import _refusal
    from connections.app_reach import PENDING_STATE, AppNotFoundError, get_app

    try:
        app = get_app(app_id, db=_db())
    except AppNotFoundError:
        raise _refusal("connected app not found", 404) from None
    config = app["config"]
    if app["state"] == PENDING_STATE or not isinstance(config, dict) or not isinstance(config.get("url"), str):
        raise _refusal("Only an approved app at a web address can be signed in to.")
    return app["name"], config, app["state"]


@signin_router.post("/servers/{app_id}/sign-in")
async def start_sign_in(
    app_id: str, request: Request, user: dict = Depends(require_user),
    token: str | None = Query(None), authorization: str | None = Header(None),
) -> dict[str, object]:
    """Open a sign-in (or reopen the pending one) and return the consent link for the person's own browser."""
    from api.connected_apps import _refusal
    from infra.accounts import token_digest
    from infra.config import CLIENT_METADATA_URL
    import tools.mcp_oauth_flow as flow
    from tools.mcp_config import MCPConfigError
    from tools.mcp_oauth_errors import SignInError

    actor = await run_in_db(_person_only, user, "sign_in", app_id)
    if not sign_in_here(request):
        raise _refusal(_NOT_HERE, 409)
    name, config, _state = await run_in_db(_load, app_id)
    raw = token_from_request(token, authorization)
    try:
        return await flow.start(
            app_id, name, config, user_id=actor[1], session_hash=token_digest(raw or ""), metadata_url=CLIENT_METADATA_URL,
        )
    except SignInError as error:
        raise HTTPException(502, detail={"failure_class": error.failure_class}) from None
    except MCPConfigError as error:
        raise _refusal(str(error)) from None


@signin_router.get("/servers/{app_id}/sign-in")
async def sign_in_status(app_id: str, user: dict = Depends(require_user)) -> dict[str, object]:
    """The pending link, or how the last attempt ended, with the provider's own words only for an unknown class."""
    import tools.mcp_oauth_flow as flow

    await run_in_db(_person_only, user, "sign_in_status", app_id)
    result = flow.status(app_id)
    link = flow.pending_link(app_id)
    return {
        **result,
        **({"authorization_url": link} if link else {}),
        **({"words": flow.last_words(app_id)} if result.get("failure_class") == "unknown" and flow.last_words(app_id) else {}),
    }


@signin_router.delete("/servers/{app_id}/sign-in")
async def cancel_sign_in(app_id: str, user: dict = Depends(require_user)) -> dict[str, object]:
    """Cancel the pending sign-in; the row goes back to Needs you to sign in."""
    import tools.mcp_oauth_flow as flow

    actor = await run_in_db(_person_only, user, "sign_in_cancel", app_id)
    return {"cancelled": await flow.cancel(app_id, user_id=actor[1])}


@signin_router.post("/servers/{app_id}/sign-out")
async def sign_out(app_id: str, user: dict = Depends(require_user)) -> dict[str, object]:
    """Revoke at the provider when it supports that, delete the sign-in here regardless, then wait for Sign in."""
    from tools.connected_apps_runtime import apply_connected_app_runtime
    from tools.mcp_oauth_signout import sign_out as run_sign_out

    actor = await run_in_db(_person_only, user, "sign_out", app_id)
    await run_in_db(_load, app_id)
    result = await run_sign_out(app_id, user_id=actor[1])
    state = await apply_connected_app_runtime("sign_out", app_id)
    return {"id": app_id, **result, **state}
