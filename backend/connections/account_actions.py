"""Sign out, Remove and Where it works for one connected account: the backend actions behind its row.

One path for every provider. Each action takes the account id and who acted,
changes only that account and never returns credential material. Sign out and
Remove delete the local sign-in first and commit the audit record, then call
the provider's revoke hook with the removed refresh token; the audit then gets
one fixed cause, never provider response text. A failed revoke still leaves
the local change in place, and the returned cause tells the person's surface
what to show. A provider without app-only revocation records ``manual_only``
in the same transaction as the local change, once.
"""

from __future__ import annotations

import sqlite3

from connections.account_provider import AccountProvider
from connections.account_state import MANUAL_ONLY, emit_lifecycle_event, record_audit, set_audit_cause, set_state
from connections.app_reach import AppReach, AppReachError, normalize_reach
from connections.connected_accounts import (
    Actor, ConnectedAccountError, _account_reach, _check_actor, get_account, provider_of, resolve_credentials, take_refresh_token,
)
from infra import key_store
from infra.database import write_transaction
from infra.db_async import run_in_db
from infra.time import utc_now_iso

SIGN_OUT_FROM = frozenset(("connected", "needs_sign_in"))
NOT_FOUND = "connected account not found"
NOT_SIGNED_IN = "account is not signed in"
INVALID_REACH = "invalid app reach"
EMPTY_REACH = "Where it works needs at least one place"
PERSON_ONLY = "only the person can widen where an account works"


def _account(account_id: str, conn: sqlite3.Connection) -> tuple[AccountProvider, dict]:
    """The account and its registered provider; a row of an unregistered provider is not found."""
    account = get_account(account_id, db=conn)
    return provider_of(account["provider"]), account


def _manual_cause(spec: AccountProvider) -> str | None:
    """The audit cause known before any provider call: ``manual_only`` when there is nothing to revoke."""
    return MANUAL_ONLY if spec.revoke is None else None


def _sign_out_locally(
    account_id: str, actor: Actor, *, db: sqlite3.Connection | None = None,
) -> tuple[AccountProvider, int, str | None]:
    """Strip the refresh token, mark the row signed out and audit it, in one transaction."""
    with write_transaction(db, scope="account_sign_out") as conn:
        spec, account = _account(account_id, conn)
        if account["state"] not in SIGN_OUT_FROM:
            raise ConnectedAccountError(NOT_SIGNED_IN)
        taken = take_refresh_token(account_id, db=conn)
        set_state(account_id, "signed_out", db=conn)
        audit_id = record_audit(account_id, "sign_out", actor, revocation_cause=_manual_cause(spec), db=conn)
    return spec, audit_id, str(taken["refresh_token"]) if taken and taken.get("refresh_token") else None


def _remove_locally(
    account_id: str, actor: Actor, *, db: sqlite3.Connection | None = None,
) -> tuple[AccountProvider, int, str | None]:
    """Audit, then delete the bundle, reach, cursor, queued screens and row, in one transaction.

    The audit row keeps the account id after the row is gone: history tables carry no foreign key.
    """
    with write_transaction(db, scope="account_remove") as conn:
        spec, account = _account(account_id, conn)
        held = resolve_credentials(account_id, db=conn)
        audit_id = record_audit(account_id, "remove", actor, revocation_cause=_manual_cause(spec), db=conn)
        key_store.drop_managed_entry(str(account["credential_key"]), db=conn)
        conn.execute("DELETE FROM connected_account_reach WHERE account_id=?", (account_id,))
        conn.execute("DELETE FROM email_screens WHERE account=?", (account_id,))
        conn.execute("DELETE FROM connected_accounts WHERE id=?", (account_id,))
    return spec, audit_id, str(held["refresh_token"]) if held and held.get("refresh_token") else None


async def _revoke(spec: AccountProvider, refresh_token: str | None) -> str:
    """Revoke one refresh token at its provider; return the fixed audit cause."""
    return MANUAL_ONLY if spec.revoke is None else await spec.revoke(refresh_token)


async def _revoke_and_audit(spec: AccountProvider, audit_id: int, refresh_token: str | None) -> str:
    cause = await _revoke(spec, refresh_token)
    if spec.revoke is not None:  # ``manual_only`` was audited with the local change.
        await run_in_db(set_audit_cause, audit_id, cause)
    return cause


async def sign_out_account(account_id: str, *, actor: Actor) -> dict[str, object]:
    """Sign one account out: no usable credential stays; the row and its client stay for Sign in."""
    _check_actor(actor)
    spec, audit_id, refresh_token = await run_in_db(_sign_out_locally, account_id, actor)
    cause = await _revoke_and_audit(spec, audit_id, refresh_token)
    emit_lifecycle_event("signed_out", account_id, spec.name)
    return {"account_id": account_id, "state": "signed_out", "revocation": cause}


async def remove_account(account_id: str, *, actor: Actor) -> dict[str, object]:
    """Remove one account: its in-process work stops and nothing of it stays locally."""
    _check_actor(actor)
    spec, audit_id, refresh_token = await run_in_db(_remove_locally, account_id, actor)
    if spec.release is not None:
        await spec.release(account_id)
    cause = await _revoke_and_audit(spec, audit_id, refresh_token)
    emit_lifecycle_event("removed", account_id, spec.name)
    return {"account_id": account_id, "removed": True, "revocation": cause}


def _widens(before: AppReach, after: AppReach) -> bool:
    if before.mode == "all":
        return False
    if after.mode == "all":
        return True
    return bool(after.project_ids - before.project_ids) or (after.outside_projects and not before.outside_projects)


def change_account_reach(
    account_id: str, reach: object, *, actor: Actor, db: sqlite3.Connection | None = None,
) -> dict[str, object]:
    """Save where one account works. Agents may only narrow it; widening needs the person.

    The saved reach is always an app reach with at least one place, so a save never writes Only CORSIA.
    """
    _check_actor(actor)
    try:
        after = normalize_reach(reach.as_dict() if isinstance(reach, AppReach) else reach)
    except AppReachError:
        raise ConnectedAccountError(INVALID_REACH) from None
    if after.mode == "selected" and not after.project_ids and not after.outside_projects:
        raise ConnectedAccountError(EMPTY_REACH)
    with write_transaction(db, scope="account_reach") as conn:
        _account(account_id, conn)
        if _widens(_account_reach(account_id, db=conn), after) and actor[0] != "person":
            raise ConnectedAccountError(PERSON_ONLY)
        from connections.app_lifecycle_support import _assert_selected_projects_exist

        _assert_selected_projects_exist(after, db=conn)
        conn.execute(
            "UPDATE connected_accounts SET reach_mode=?,outside_projects=?,updated_at=? WHERE id=?",
            (after.mode, int(after.outside_projects), utc_now_iso(), account_id),
        )
        conn.execute("DELETE FROM connected_account_reach WHERE account_id=?", (account_id,))
        conn.executemany(
            "INSERT INTO connected_account_reach(account_id,project_id) VALUES (?,?)",
            ((account_id, project_id) for project_id in sorted(after.project_ids)),
        )
        return get_account(account_id, db=conn)
