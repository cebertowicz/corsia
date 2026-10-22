"""A mail-host row's own controls: Try again and Edit server.

Both start a check that logs in to the host with the app password, and a server name decides
where that password is sent, so both are the person's alone and need their CORSIA password in
the same request (the Keys ``verified_write`` gate, H17). No agent-callable route reaches them.
Bodies are parsed here so a refusal never echoes the input; every refusal text is fixed.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, Request

from api.connected_account_rows import NOT_FOUND, _row
from api.connected_app_access import refusal as not_found_refusal
from api.key_session_guard import audit_key_write
from api.key_write_guard import refusal, verified_write
from api.keys import _body, _human_session
from connections.connected_accounts import MAILHOST_PROVIDER, credential_key
from connections.mailhost import check, store
from connections.mailhost.endpoints import Endpoints, EndpointsRefused, parse_caldav, parse_server
from infra.database import get_db
from infra.db_async import run_in_db

mail_host_router = APIRouter()


def _endpoints(account_id: str) -> Endpoints:
    """The row's settings, or the same 404 an unknown account gets."""
    db = get_db()
    try:
        endpoints = store.read(account_id, db=db)[0]
    finally:
        db.close()
    if endpoints is None:
        raise not_found_refusal(NOT_FOUND, 404)
    return endpoints


def _new_settings(endpoints: Endpoints, body: dict[str, Any]) -> Endpoints:
    """The typed server names as settings for a check: a typed-in row from here on, nothing proven yet."""
    try:
        return replace(
            endpoints, known=False, imap=parse_server(body.get("imap"), field_name="imap"),
            smtp=parse_server(body.get("smtp"), field_name="smtp"),
            caldav=parse_caldav(body["caldav"]) if str(body.get("caldav") or "").strip() else None,
            calendars=(), imap_username=None, caldav_username=None,
        )
    except EndpointsRefused as exc:
        raise refusal(400, "invalid_servers", str(exc)) from None


async def _checked(
    account_id: str, request: Request, session: tuple[dict, str], action: str, replacement: bool,
) -> dict[str, Any]:
    user, token = session
    body = await _body(request)
    endpoints = await run_in_db(_endpoints, account_id)
    new = _new_settings(endpoints, body) if replacement else None
    await verified_write(token, user["id"], body.get("password"), lambda: None)
    if not await check.start_check(account_id, new):
        raise not_found_refusal(NOT_FOUND, 404)
    audit_key_write(user, kind="added", name=credential_key(account_id, MAILHOST_PROVIDER), action=action)
    return {"row": await run_in_db(_row, account_id)}


@mail_host_router.post("/accounts/{account_id}/mailhost/check")
async def try_again(
    account_id: str, request: Request, session: tuple[dict, str] = Depends(_human_session),
) -> dict[str, Any]:
    """Try again: run the check once more with the saved servers and app password."""
    return await _checked(account_id, request, session, "check", replacement=False)


@mail_host_router.put("/accounts/{account_id}/mailhost/servers")
async def edit_servers(
    account_id: str, request: Request, session: tuple[dict, str] = Depends(_human_session),
) -> dict[str, Any]:
    """Edit server: save the typed IMAP, SMTP and calendar addresses, then check against them."""
    return await _checked(account_id, request, session, "servers", replacement=True)
