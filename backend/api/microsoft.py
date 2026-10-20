"""Microsoft connected-account setup and status routes, separate from hosted MCP apps."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from api.connected_app_access import audit_actor
from connections.app_reach import AppReach
from connections.microsoft import oauth
from connections.microsoft.errors import ACCOUNT_NOT_FOUND, REGISTRATION_MISCONFIGURED, MicrosoftError
from infra.auth import is_person_session, require_user
from infra.db_async import run_in_db

router = APIRouter(prefix="/api/microsoft", tags=["microsoft"])
ALL_PROJECTS = AppReach(mode="all", project_ids=frozenset(), outside_projects=False)
MAX_BODY_BYTES = 2048
NOT_FOUND = "That account isn't connected."
PERSON_CAUSE_COPY = {
    "admin_approval_required": "Your organization needs to approve CORSIA before it can connect this account. Send this to your IT admin, then press Sign in.",
    "consent_cancelled": "Sign-in was cancelled. Press Sign in to try again.",
    "refresh_token_invalid": "Microsoft signed you out, so agents can’t use this account until you sign in again.",
    "provider_unavailable": "Microsoft had a problem with sign-in. Try again.",
    REGISTRATION_MISCONFIGURED: "Your Microsoft app for CORSIA isn't set up right. Ask CORSIA to fix it, then press Sign in.",
}


def _body_field(body: object, key: str) -> str | None:
    value = body.get(key) if isinstance(body, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _agent_thread_id(user: dict) -> str:
    from api.connected_apps import _agent_thread
    from infra.database import get_db
    db = get_db()
    try:
        thread_id, _scope = _agent_thread(user, db)
        return thread_id
    finally:
        db.close()


@router.post("/connect")
async def connect(request: Request, user: dict = Depends(require_user)) -> dict[str, Any]:
    """Start setup or reauthenticate one visible Microsoft account row."""
    try:
        raw = await request.body()
        body = json.loads(raw) if raw and len(raw) <= MAX_BODY_BYTES else {}
    except ValueError:
        body = {}
    account_id = _body_field(body, "account_id")
    person = is_person_session(user)
    adding_thread_id = None if person else await run_in_db(_agent_thread_id, user)
    try:
        url, signing_in = await oauth.start_sign_in(
            _body_field(body, "client_id"), _body_field(body, "kind"), _body_field(body, "tenant_id"),
            actor=audit_actor(user), account_id=account_id, adding_thread_id=adding_thread_id,
            reach=ALL_PROJECTS if person else None,
        )
    except MicrosoftError as exc:
        if exc.cause == ACCOUNT_NOT_FOUND:
            raise HTTPException(404, {"cause": exc.cause, "message": NOT_FOUND}) from None
        if person and (copy := PERSON_CAUSE_COPY.get(exc.cause)) is not None:
            raise HTTPException(400, copy) from None
        raise HTTPException(400, {"cause": exc.cause, "message": exc.message}) from None
    return {"url": url, "account_id": signing_in}


@router.get("/status")
async def status(user: dict = Depends(require_user)) -> dict[str, object]:
    person = is_person_session(user)
    result = await run_in_db(oauth.accounts_status, None if person else await run_in_db(_agent_thread_id, user))
    if person and result.get("cause") == REGISTRATION_MISCONFIGURED:
        return {**result, "message": PERSON_CAUSE_COPY[REGISTRATION_MISCONFIGURED]}
    return result

# Account-row Sign out and Remove use the established person-only controls in
# ``api.connected_account_rows``; this router owns only Microsoft setup and status.
