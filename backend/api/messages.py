"""Messages connected-account route: the one Sign in on a signed-out Messages row.

Messages has no sign-in step: Sign in only turns the row back on where it already works.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from api.connected_app_access import audit_actor
from connections.messages import account
from infra.auth import is_person_session, require_user

router = APIRouter(prefix="/api/messages", tags=["messages"])
MAX_BODY_BYTES = 2048
NOT_FOUND = "That isn't a Messages row."
PERSON_ONLY = "Only the signed-in person can turn Messages back on; open Connected apps."


@router.post("/connect")
async def connect(request: Request, user: dict = Depends(require_user)) -> dict[str, Any]:
    """Turn one Messages row back on; there is nothing to open, so ``url`` is always empty."""
    if not is_person_session(user):
        raise HTTPException(403, PERSON_ONLY, headers={"X-Corsia-Agent-Remedy": PERSON_ONLY})
    try:
        raw = await request.body()
        body = json.loads(raw) if raw and len(raw) <= MAX_BODY_BYTES else {}
    except ValueError:
        body = {}
    value = body.get("account_id") if isinstance(body, dict) else None
    account_id = value.strip() if isinstance(value, str) else ""
    if not account_id or not await account.turn_on(account_id, audit_actor(user)):
        raise HTTPException(404, NOT_FOUND)
    return {"url": "", "account_id": account_id}
