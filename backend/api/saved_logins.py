"""The person's own saved-login routes: the save offer, and the list of saved logins.

Every route needs the signed-in person's session (``require_human_user``); an
agent's token is refused. A route that changes something (Save, Not now,
Delete) takes that session from the ``Authorization`` header only, as the key
store's own writes do: a session in the URL reaches logs, referrers and
browser history, so it authorizes the reads here and no change. The offer routes also need a thread the person owns,
and resolve its current browser session on the server, so an offer from an
earlier browser session never shows. No route carries a password. Save and Not
now read only ``thread_id`` from their body and refuse anything else with fixed
text, so no refusal echoes what was sent.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request

from infra import saved_login_offers, saved_logins
from infra.auth import require_human_user, require_user_header_only
from infra.db_async import run_in_db
from infra.key_store_values import KeyStoreError
from infra.queries import get_thread_for_user

router = APIRouter(prefix="/api/saved-logins", tags=["saved-logins"])

_NO_THREAD = "No chat has that id."
_BAD_BODY = "This request needs the chat it's for."
#: Largest body read: a JSON object holding one thread id, which is a 36-character UUID, with ample room.
MAX_BODY_BYTES = 1024


def require_person_write(user: dict = Depends(require_user_header_only)) -> dict:
    """The signed-in person's session for a change, read from the ``Authorization`` header only."""
    return require_human_user(user)


async def _thread_id(request: Request) -> str:
    """The ``thread_id`` from a small JSON body; any other shape is a fixed refusal."""
    raw = await request.body()
    try:
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError
        thread_id = json.loads(raw).get("thread_id")
    except (ValueError, AttributeError):
        raise HTTPException(400, _BAD_BODY) from None
    if not isinstance(thread_id, str) or not thread_id:
        raise HTTPException(400, _BAD_BODY)
    return thread_id


async def _session_key(thread_id: str, user: dict) -> str | None:
    """The current browser session key of a thread the person owns, or None when no browser is open."""
    if not await run_in_db(get_thread_for_user, thread_id, user["id"]):
        raise HTTPException(404, _NO_THREAD)
    return saved_login_offers.session_key_for(thread_id)


@router.get("/offer")
async def get_offer(thread_id: str, user: dict = Depends(require_human_user)) -> dict:
    """The thread's confirmed save offer (site, username, whether it updates a saved login), or null.

    Answers null, not an error, while no confirmed offer is held; safe to repeat.
    """
    view = saved_login_offers.pending_offer(await _session_key(thread_id, user))
    return {"offer": asdict(view) if view is not None else None}


@router.post("/offer/save")
async def save_offer(request: Request, user: dict = Depends(require_person_write)) -> dict:
    """Save the thread's confirmed offer as the signed-in person, then drop it; 409 when none is held."""
    key = await _session_key(await _thread_id(request), user)
    try:
        await run_in_db(saved_login_offers.save_offer, key, saved_by=user["id"])
    except KeyStoreError as refused:
        status = 409 if str(refused) == saved_login_offers.NO_OFFER else 400
        raise HTTPException(status, str(refused)) from None
    return {"saved": True}


@router.post("/offer/not-now")
async def not_now(request: Request, user: dict = Depends(require_person_write)) -> dict:
    """Drop the thread's offer, if any. Safe to repeat."""
    saved_login_offers.drop_offer(await _session_key(await _thread_id(request), user))
    return {"offer": None}


@router.get("")
async def list_saved_logins(_user: dict = Depends(require_human_user)) -> dict:
    """Every saved login: site, username, status, who saved it and when it was last used. Never a password."""
    return {"logins": [asdict(login) for login in await run_in_db(saved_logins.list_logins)]}


@router.delete("/{login_id}")
async def delete_saved_login(login_id: str, _user: dict = Depends(require_person_write)) -> dict:
    """Delete one saved login and its password."""
    try:
        await run_in_db(saved_logins.delete_login, login_id)
    except KeyStoreError as refused:
        raise HTTPException(404, str(refused)) from None
    return {"deleted": login_id}
