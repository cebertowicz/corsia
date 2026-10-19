"""Google accounts as rows in the Connected apps list, and the routes for one row's controls.

An account row is a different ``kind`` from a hosted app (``"account"``, not
``"hosted_app"``): it has an address, capabilities and an account state, and
its reach is the account's own. A row never carries a credential, a key name,
a token, a cursor or provider text. Every refusal here is a fixed text.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends

from api.connected_app_access import audit_actor, refusal
from infra.auth import is_person_session, require_user
from infra.database import get_db
from infra.db_async import run_in_db

log = logging.getLogger(__name__)
accounts_router = APIRouter()

NOT_FOUND = "That Google account isn't connected."
NOT_SIGNED_IN = "That Google account is already signed out."
NOT_YOURS = "This Google account is available only in CORSIA. Ask the person who connected it to change Where it works."
PERSON_ONLY_CONTROLS = "Only the signed-in person can sign out or remove a Google account; open Connected apps."
PERSON_ONLY = "Only the signed-in person can widen where an account works; open Connected apps."
INVALID_REACH = "Where it works needs a valid choice of places."
EMPTY_REACH = "Where it works needs at least one place."
CHANGE_FAILED = "Couldn't change this Google account. Try again."


def account_row(account: dict[str, object], *, db: sqlite3.Connection) -> dict[str, object]:
    """One account's value-free list row."""
    from connections.connected_accounts import resolve_google_credentials

    state = str(account["state"])
    if state == "connected" and resolve_google_credentials(str(account["id"]), db=db) is None:
        state = "needs_sign_in"
    return {
        "kind": "account", "id": account["id"], "provider": account["provider"], "address": account["address"],
        "capabilities": account["capabilities"], "state": state, "reach": account["reach"],
    }


def list_account_rows(thread_id: str | None, *, db: sqlite3.Connection) -> list[dict[str, object]]:
    """Every Google account for the person (``thread_id`` None); for an agent only those that reach its thread."""
    from connections.google.accounts import visible_google_accounts

    return [account_row(account, db=db) for account in visible_google_accounts(thread_id, db=db)]


def _authorize(account_id: str, user: dict, *, person_only: bool = False) -> None:
    """404 for an unknown or non-Google id; then, for an agent, 403 at once when the control is
    ``person_only`` (Sign out, Remove), else 403 unless the account reaches its thread (Where it works)."""
    from connections.connected_accounts import (
        GOOGLE_PROVIDER, ConnectedAccountError, account_reaches_thread, get_account,
    )

    db = get_db()
    try:
        try:
            if get_account(account_id, db=db)["provider"] != GOOGLE_PROVIDER:
                raise ConnectedAccountError("connected account not found")
        except ConnectedAccountError:
            raise refusal(NOT_FOUND, 404) from None
        if is_person_session(user):
            return
        if person_only:
            raise refusal(PERSON_ONLY_CONTROLS, 403)
        from api.connected_apps import _agent_thread  # Imported here: that module includes this one's router.

        thread_id, _scope = _agent_thread(user, db)
        if not account_reaches_thread(account_id, thread_id, db=db):
            raise refusal(NOT_YOURS, 403)
    finally:
        db.close()


def _row(account_id: str) -> dict[str, object]:
    from connections.connected_accounts import get_account

    db = get_db()
    try:
        return account_row(get_account(account_id, db=db), db=db)
    finally:
        db.close()


def _error(exc: Exception) -> Exception:
    """Map an action's failure to a fixed refusal; unexpected failures are logged, never echoed."""
    from connections.connected_accounts import ConnectedAccountError
    from connections.google import account_actions

    if not isinstance(exc, ConnectedAccountError):
        log.exception("Google account change failed")
        return refusal(CHANGE_FAILED, 500)
    fixed = {
        account_actions.NOT_FOUND: refusal(NOT_FOUND, 404),
        account_actions.NOT_SIGNED_IN: refusal(NOT_SIGNED_IN, 409),
        account_actions.INVALID_REACH: refusal(INVALID_REACH),
        account_actions.EMPTY_REACH: refusal(EMPTY_REACH),
        account_actions.PERSON_ONLY: refusal(PERSON_ONLY, 403),
    }
    return fixed.get(str(exc)) or refusal(CHANGE_FAILED)


@accounts_router.post("/accounts/{account_id}/sign-out")
async def sign_out_account(account_id: str, user: dict = Depends(require_user)) -> dict[str, object]:
    """Sign out one account. Returns its updated row and the fixed revocation cause."""
    from connections.google.account_actions import sign_out_google_account

    await run_in_db(lambda: _authorize(account_id, user, person_only=True))
    try:
        result = await sign_out_google_account(account_id, actor=audit_actor(user))
    except Exception as exc:
        raise _error(exc) from None
    return {"row": await run_in_db(_row, account_id), "revocation": result["revocation"]}


@accounts_router.delete("/accounts/{account_id}")
async def remove_account(account_id: str, user: dict = Depends(require_user)) -> dict[str, object]:
    """Remove one account and everything local to it. Returns ``removed`` and the fixed revocation cause."""
    from connections.google.account_actions import remove_google_account

    await run_in_db(lambda: _authorize(account_id, user, person_only=True))
    try:
        result = await remove_google_account(account_id, actor=audit_actor(user))
    except Exception as exc:
        raise _error(exc) from None
    return {"id": account_id, "removed": True, "revocation": result["revocation"]}


@accounts_router.put("/accounts/{account_id}/reach")
async def change_account_reach(account_id: str, body: dict[str, Any], user: dict = Depends(require_user)) -> dict[str, object]:
    """Save where one account works. Only the person can widen it; the server decides that from the session."""
    from connections.google.account_actions import change_google_account_reach

    if set(body) != {"reach"}:
        raise refusal("reach changes need reach only")
    await run_in_db(_authorize, account_id, user)
    try:
        await run_in_db(lambda: change_google_account_reach(account_id, body["reach"], actor=audit_actor(user)))
    except Exception as exc:
        raise _error(exc) from None
    return {"row": await run_in_db(_row, account_id)}
