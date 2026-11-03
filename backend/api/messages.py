"""Messages connected-account route: the person's Sign in turns the Messages app on.

Messages has no sign-in step. Sign in only checks that macOS lets CORSIA read Messages, then turns the
app on where it already works.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

import tools.messages_texts as texts
from api.connected_app_access import audit_actor
from connections.messages import account
from infra.auth import is_person_session, require_user
from tools import messages_db

router = APIRouter(prefix="/api/messages", tags=["messages"])
MAX_BODY_BYTES = 2048
BAD_REQUEST = "Couldn’t turn on Messages: the request was incomplete. Reload Settings › Connected apps and try again."
NOT_FOUND = "Messages isn’t in Connected apps. Reload Settings › Connected apps."
PERSON_ONLY = "Only the signed-in person can turn Messages on; open Connected apps."
NO_FDA = (
    "CORSIA can’t read your Messages. Open System Settings › Privacy & Security › Full Disk Access, "
    "turn on CORSIA, quit and reopen CORSIA, then press Sign in again."
)
NO_HISTORY = "There are no Messages on this computer. Open Messages and sign in, then press Sign in again."
NOT_A_MAC = "Messages is only available on a Mac running CORSIA."
UNREADABLE = "CORSIA can’t read Messages on this version of macOS. Update CORSIA, then press Sign in again."
BUSY = "CORSIA couldn’t read Messages just now. Press Sign in again in a moment."
FOR_THE_PERSON = {
    texts.NO_FDA: NO_FDA, texts.NO_HISTORY: NO_HISTORY, texts.NOT_A_MAC: NOT_A_MAC, texts.UNREADABLE: BUSY,
}


async def _account_id(request: Request) -> str:
    """The ``account_id`` in a small JSON body; any other body is a 400."""
    try:
        raw = await request.body()
        body = json.loads(raw) if raw and len(raw) <= MAX_BODY_BYTES else None
    except ValueError:
        body = None
    value = body.get("account_id") if isinstance(body, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(400, BAD_REQUEST)
    return value.strip()


@router.post("/connect")
async def connect(request: Request, user: dict = Depends(require_user)) -> dict[str, Any]:
    """Turn one Messages app on once CORSIA can read it; there is nothing to open, so ``url`` is always empty."""
    if not is_person_session(user):
        raise HTTPException(403, PERSON_ONLY, headers={"X-Corsia-Agent-Remedy": PERSON_ONLY})
    account_id = await _account_id(request)
    state = await account.state_of(account_id)
    if state is None:
        raise HTTPException(404, NOT_FOUND)
    if state != "connected":
        try:
            await asyncio.to_thread(messages_db.check_readable)
        except messages_db.MessagesError as exc:
            raise HTTPException(409, FOR_THE_PERSON.get(str(exc), UNREADABLE)) from None
    if not await account.turn_on(account_id, audit_actor(user)):
        raise HTTPException(404, NOT_FOUND)
    return {"url": "", "account_id": account_id}
