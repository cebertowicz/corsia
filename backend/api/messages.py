"""Messages connected-account route: Enable checks access and turns Messages on."""
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
    "macOS hasn’t given CORSIA Full Disk Access, so it can’t read Messages.\n"
    "Full Disk Access lets CORSIA, and commands agents run here after review, read any protected file on this "
    "computer, such as Mail and Photos.\n"
    "To turn it on: System Settings › Privacy & Security › Full Disk Access, switch on CORSIA, reopen CORSIA, "
    "then press Enable. To undo it, switch CORSIA off there."
)
NO_HISTORY = (
    "There’s no Messages history on this computer. Open the Messages app and sign in with your Apple Account, "
    "then press Enable."
)
NOT_A_MAC = "Messages is only available on a Mac running CORSIA."
#: The Messages database changed shape (``texts.SCHEMA``); unlike ``texts.UNREADABLE``, waiting won't help.
NEEDS_UPDATE = "CORSIA couldn’t read Messages on this version of macOS. Update CORSIA, then press Enable."
BUSY = "CORSIA couldn’t read Messages just now. Press Enable in a moment."
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
            raise HTTPException(409, FOR_THE_PERSON.get(str(exc), NEEDS_UPDATE)) from None
    if not await account.turn_on(account_id, audit_actor(user)):
        raise HTTPException(404, NOT_FOUND)
    return {"url": "", "account_id": account_id}
