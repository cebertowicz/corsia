"""Connected accounts (Google, Microsoft) as rows in the Connected apps list, and the routes for one row's controls.

An account row is a different ``kind`` from a hosted app (``"account"``, not
``"hosted_app"``): it has an address, capabilities and an account state, and
its reach is the account's own. A row never carries a credential, a key name,
a token, a cursor or provider text. Every refusal here is a fixed text; once
the caller may see the row, it names the row's provider.
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

# Answered before the caller may know the row exists, so it names no provider: an agent gets the
# same text for an id outside its reach as for an unknown one.
NOT_FOUND = "That account isn't connected."
NOT_SIGNED_IN = "That {provider} account is already signed out."
PERSON_ONLY_CONTROLS = "Only the signed-in person can sign out or remove a {provider} account; open Connected apps."
PERSON_ONLY = "Only the signed-in person can widen where an account works; open Connected apps."
INVALID_REACH = "Where it works needs a valid choice of places."
EMPTY_REACH = "Where it works needs at least one place."
CHANGE_FAILED = "Couldn't change this {provider} account. Try again."


def account_row(account: dict[str, object], *, db: sqlite3.Connection, detail: dict[str, object] | None = None) -> dict[str, object]:
    """One account's value-free list row."""
    from connections.connected_accounts import account_registration, provider_of, resolve_credentials

    state = str(account["state"])
    if state == "connected" and resolve_credentials(str(account["id"]), db=db) is None:
        state = "needs_sign_in"
    provider = provider_of(str(account["provider"]))
    return {
        "kind": "account", "id": account["id"], "provider": account["provider"], "provider_display_name": provider.display_name, "address": account["address"],
        "capabilities": account["capabilities"], "state": state, "reach": account["reach"],
        "account_kind": account["account_kind"], "cause": account["state_cause"], "scopes": list(provider.display_scopes),
        **account_registration(str(account["id"]), db=db),
        **(detail if detail is not None else (provider.row_detail(str(account["id"]), db) if provider.row_detail else {})),
    }


def list_account_rows(thread_id: str | None, *, db: sqlite3.Connection) -> list[dict[str, object]]:
    """Every connected account for the person (``thread_id`` None); for an agent only those that reach its thread."""
    from connections.connected_accounts import visible_accounts

    accounts = visible_accounts(thread_id, db=db)
    details: dict[str, dict[str, object]] = {}
    by_provider: dict[str, list[str]] = {}
    for account in accounts:
        by_provider.setdefault(str(account["provider"]), []).append(str(account["id"]))
    from connections.connected_accounts import provider_of
    for provider_name, account_ids in by_provider.items():
        provider = provider_of(provider_name)
        if provider.row_details:
            details.update(provider.row_details(account_ids, db))
    return [account_row(account, db=db, detail=details.get(str(account["id"]))) for account in accounts]


def _authorize(account_id: str, user: dict, *, person_only: bool = False) -> str:
    """404 unless the caller may see the account (the one visibility rule), so an agent gets the same
    answer for an id outside its reach as for an unknown one; then, for an agent, 403 when the control
    is ``person_only`` (Sign out, Remove). Returns the row's provider display name."""
    from connections.connected_accounts import provider_of, visible_account

    db = get_db()
    try:
        thread_id: str | None = None
        if not is_person_session(user):
            from api.connected_apps import _agent_thread  # Imported here: that module includes this one's router.

            thread_id, _scope = _agent_thread(user, db)
        account = visible_account(account_id, thread_id, db=db)
        if account is None:
            raise refusal(NOT_FOUND, 404)
        provider = provider_of(account["provider"]).display_name
        if thread_id is not None and person_only:
            raise refusal(PERSON_ONLY_CONTROLS.format(provider=provider), 403)
        return provider
    finally:
        db.close()


def _row(account_id: str) -> dict[str, object]:
    from connections.connected_accounts import get_account

    db = get_db()
    try:
        return account_row(get_account(account_id, db=db), db=db)
    finally:
        db.close()


def _error(exc: Exception, provider: str) -> Exception:
    """Map an action's failure to a fixed refusal; unexpected failures are logged, never echoed."""
    from connections import account_actions
    from connections.connected_accounts import ConnectedAccountError

    if not isinstance(exc, ConnectedAccountError):
        log.exception("Connected account change failed")
        return refusal(CHANGE_FAILED.format(provider=provider), 500)
    fixed = {
        account_actions.NOT_FOUND: refusal(NOT_FOUND, 404),
        account_actions.NOT_SIGNED_IN: refusal(NOT_SIGNED_IN.format(provider=provider), 409),
        account_actions.INVALID_REACH: refusal(INVALID_REACH),
        account_actions.EMPTY_REACH: refusal(EMPTY_REACH),
        account_actions.PERSON_ONLY: refusal(PERSON_ONLY, 403),
    }
    return fixed.get(str(exc)) or refusal(CHANGE_FAILED.format(provider=provider))


@accounts_router.post("/accounts/{account_id}/sign-out")
async def sign_out_account(account_id: str, user: dict = Depends(require_user)) -> dict[str, object]:
    """Sign out one account. Returns its updated row and the fixed revocation cause."""
    from connections import account_actions

    provider = await run_in_db(lambda: _authorize(account_id, user, person_only=True))
    try:
        result = await account_actions.sign_out_account(account_id, actor=audit_actor(user))
    except Exception as exc:
        raise _error(exc, provider) from None
    return {"row": await run_in_db(_row, account_id), "revocation": result["revocation"]}


@accounts_router.delete("/accounts/{account_id}")
async def remove_account(account_id: str, user: dict = Depends(require_user)) -> dict[str, object]:
    """Remove one account and everything local to it. Returns ``removed`` and the fixed revocation cause."""
    from connections import account_actions

    provider = await run_in_db(lambda: _authorize(account_id, user, person_only=True))
    try:
        result = await account_actions.remove_account(account_id, actor=audit_actor(user))
    except Exception as exc:
        raise _error(exc, provider) from None
    return {"id": account_id, "removed": True, "revocation": result["revocation"]}


@accounts_router.put("/accounts/{account_id}/reach")
async def change_account_reach(account_id: str, body: dict[str, Any], user: dict = Depends(require_user)) -> dict[str, object]:
    """Save where one account works. Only the person can widen it; the server decides that from the session."""
    from connections import account_actions

    if set(body) != {"reach"}:
        raise refusal("reach changes need reach only")
    provider = await run_in_db(_authorize, account_id, user)
    try:
        await run_in_db(lambda: account_actions.change_account_reach(account_id, body["reach"], actor=audit_actor(user)))
    except Exception as exc:
        raise _error(exc, provider) from None
    return {"row": await run_in_db(_row, account_id)}
