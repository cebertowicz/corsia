"""Microsoft connected-account setup and status routes, separate from hosted MCP apps."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from api.connected_app_access import audit_actor
from connections.app_reach import AppReach
from connections.microsoft import oauth
from connections.microsoft.errors import MicrosoftError
from infra.auth import is_person_session, require_user
from infra.db_async import run_in_db

router = APIRouter(prefix="/api/microsoft", tags=["microsoft"])
ALL_PROJECTS = AppReach(mode="all", project_ids=frozenset(), outside_projects=False)


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
    try:
        raw = await request.body()
        body = json.loads(raw) if raw and len(raw) <= 2048 else {}
    except ValueError:
        body = {}
    person = is_person_session(user)
    adding_thread_id = None if person else await run_in_db(_agent_thread_id, user)
    try:
        url, account_id = await oauth.start_sign_in(
            body.get("client_id") if isinstance(body, dict) else None,
            body.get("kind") if isinstance(body, dict) else None,
            body.get("tenant_id") if isinstance(body, dict) else None,
            actor=audit_actor(user), adding_thread_id=adding_thread_id,
            reach=ALL_PROJECTS if person else None,
        )
    except MicrosoftError as exc:
        raise HTTPException(400, exc.cause) from None
    return {"url": url, "account_id": account_id}


@router.get("/status")
async def status(user: dict = Depends(require_user)) -> dict[str, object]:
    return await run_in_db(oauth.accounts_status, None if is_person_session(user) else await run_in_db(_agent_thread_id, user))

# Account-row Sign out and Remove use the established person-only controls in
# ``api.connected_account_rows``; this router owns only Microsoft setup and status.
