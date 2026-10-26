"""Microsoft connected-account setup and status routes, separate from hosted MCP apps."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from api.connected_app_access import audit_actor
from connections.app_reach import AppReach
from connections.microsoft import oauth
from connections.microsoft.errors import ACCOUNT_NOT_FOUND, MicrosoftError, person_message
from infra.auth import is_person_session, require_user
from infra.db_async import run_in_db

router = APIRouter(prefix="/api/microsoft", tags=["microsoft"])
ALL_PROJECTS = AppReach(mode="all", project_ids=frozenset(), outside_projects=False)
MAX_BODY_BYTES = 2048
NOT_FOUND = "That account isn't connected."


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
            starting_thread_id=adding_thread_id, reach=ALL_PROJECTS if person else None,
        )
    except MicrosoftError as exc:
        if exc.cause == ACCOUNT_NOT_FOUND:
            raise HTTPException(404, NOT_FOUND) from None
        raise HTTPException(400, person_message(exc.cause)) from None
    return {"url": url, "account_id": signing_in}


# Account-row Sign out and Remove use the established person-only controls in
# ``api.connected_account_rows``; this router owns only Microsoft setup.
