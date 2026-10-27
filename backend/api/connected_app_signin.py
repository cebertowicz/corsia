"""Sign in to, and out of, a hosted connected app. Person only: an agent is refused and recorded."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from api.connected_app_access import person_only, refusal as _refusal
from infra.auth import require_user, token_from_request
from infra.db_async import run_in_db
from infra.local_connection import is_direct_loopback

signin_router = APIRouter()
_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "[::1]", "::1"}
_NOT_PERSON = "Only the signed-in person can sign in to an app or sign out of it; open Connected apps."
_NOT_HERE = "You can only sign in on the computer CORSIA runs on. Open CORSIA there and press Sign in."


def _db():
    from infra.database import get_db
    return get_db()


def sign_in_here(request: Request) -> bool:
    """Whether this browser is on the computer running CORSIA, so the loopback callback can reach the backend.

    Local trust is :func:`infra.local_connection.is_direct_loopback`; the Host check only adds that the
    browser addressed CORSIA by a loopback name, which the provider's redirect back needs.
    """
    host = request.headers.get("host") or ""
    name = host[: host.index("]") + 1] if host.startswith("[") else host.rsplit(":", 1)[0]
    return is_direct_loopback(request) and name.lower() in _LOOPBACK_NAMES


def _person_only(user: dict, action: str, app_id: str) -> tuple[str, str]:
    return person_only(user, action, app_id, copy=_NOT_PERSON, db=_db())


def _session_hash(token: str | None, authorization: str | None) -> str:
    """The digest of the caller's own session credential: what a pending flow is bound to."""
    from infra.accounts import token_digest

    return token_digest(token_from_request(token, authorization) or "")


def _load(app_id: str) -> tuple[str, dict[str, Any], str]:
    from connections.app_reach import PENDING_STATE, AppNotFoundError, get_app

    try:
        app = get_app(app_id, db=_db())
    except AppNotFoundError:
        raise _refusal("connected app not found", 404) from None
    config = app["config"]
    if app["state"] in {PENDING_STATE, "expired"} or not isinstance(config, dict) or not isinstance(config.get("url"), str):
        raise _refusal("Only an approved app at a web address can be signed in to.")
    return app["name"], config, app["state"]


@signin_router.post("/servers/{app_id}/sign-in")
async def start_sign_in(
    app_id: str, request: Request, user: dict = Depends(require_user),
    token: str | None = Query(None), authorization: str | None = Header(None),
) -> dict[str, object]:
    """Open this session's sign-in (or reopen its pending one) and return the consent link for its own browser."""
    from infra.config import CLIENT_METADATA_URL
    import tools.mcp_oauth_flow as flow
    from tools.mcp_config import MCPConfigError
    from tools.mcp_oauth_errors import SignInError

    actor = await run_in_db(_person_only, user, "sign_in", app_id)
    if not sign_in_here(request):
        raise _refusal(_NOT_HERE, 409)
    name, config, _state = await run_in_db(_load, app_id)
    try:
        return await flow.start(
            app_id, name, config, user_id=actor[1], session_hash=_session_hash(token, authorization),
            metadata_url=CLIENT_METADATA_URL,
        )
    except SignInError as error:
        raise HTTPException(502, detail={"failure_class": error.failure_class}) from None
    except MCPConfigError as error:
        raise _refusal(str(error)) from None


@signin_router.get("/servers/{app_id}/sign-in")
async def sign_in_status(
    app_id: str, user: dict = Depends(require_user),
    token: str | None = Query(None), authorization: str | None = Header(None),
) -> dict[str, object]:
    """How the sign-in stands: the pending link only for its own session, the provider's words only when unknown."""
    import tools.mcp_oauth_flow as flow

    actor = await run_in_db(_person_only, user, "sign_in_status", app_id)
    result = flow.status(app_id)
    link = flow.pending_link(app_id, user_id=actor[1], session_hash=_session_hash(token, authorization))
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
    return {"cancelled": await flow.cancel(app_id, actor=actor)}


@signin_router.post("/servers/{app_id}/sign-out")
async def sign_out(app_id: str, user: dict = Depends(require_user)) -> dict[str, object]:
    """End the app's sign-in here, then revoke it at the provider when it supports that, then wait for Sign in."""
    from tools.connected_apps_runtime import apply_connected_app_runtime
    from tools.mcp_oauth_signout import sign_out as run_sign_out

    actor = await run_in_db(_person_only, user, "sign_out", app_id)
    await run_in_db(_load, app_id)
    result = await run_sign_out(app_id, actor=actor)
    state = await apply_connected_app_runtime("sign_out", app_id)
    return {"id": app_id, **result, **state}
