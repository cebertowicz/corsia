"""Sign out, Remove and Where it works for one Google account: the backend actions behind its row.

Each action takes the account id and who acted, changes only that account and
never returns credential material. Sign out and Remove delete the local
sign-in first and commit the audit record, then revoke at Google with the
removed refresh token; the audit then gets one fixed cause, never Google's
response text. A failed revoke still leaves the local change in place, and the
returned cause tells the person's surface what to show.

The cause is one of ``REVOCATION_CAUSES``. Only ``MANUAL_STEP_CAUSES`` (the
provider could not be asked, or refused) show the manual Google Account step;
``revoked`` shows nothing more, and ``not_signed_in`` means no sign-in was held
because Sign out already revoked it, so there is nothing for the person to do.
"""

from __future__ import annotations

import sqlite3

import httpx

from connections.app_reach import AppReach, AppReachError, normalize_reach
from connections.connected_accounts import (
    GOOGLE_PROVIDER, Actor, ConnectedAccountError, _account_reach, _check_actor, corsia_only_reach,
    get_account, record_audit, resolve_google_credentials, set_audit_cause, set_state,
    take_google_refresh_token,
)
from connections.google.client import TIMEOUT_SECONDS
from infra import key_store
from infra.database import write_transaction
from infra.db_async import run_in_db
from infra.time import utc_now_iso

REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SIGN_OUT_FROM = frozenset(("connected", "needs_sign_in"))
NOT_FOUND = "connected account not found"
NOT_SIGNED_IN = "account is not signed in"
INVALID_REACH = "invalid app reach"
EMPTY_REACH = "Where it works needs at least one place"
PERSON_ONLY = "only the person can widen where an account works"
MANUAL_STEP_CAUSES = frozenset(("provider_unreachable", "provider_timeout", "provider_rejected", "provider_unexpected"))


async def _revoke(refresh_token: str | None) -> str:
    """Revoke one refresh token at Google; return the fixed audit cause."""
    if not refresh_token:
        return "not_signed_in"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
            response = await http.post(REVOKE_URL, data={"token": refresh_token})
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        return "provider_unreachable"
    except httpx.TimeoutException:
        return "provider_timeout"
    except httpx.HTTPError:
        return "provider_unexpected"
    if response.status_code == 200:
        return "revoked"
    return "provider_rejected" if 400 <= response.status_code < 500 else "provider_unexpected"


def _google_row(account_id: str, conn: sqlite3.Connection) -> dict:
    account = get_account(account_id, db=conn)
    if account["provider"] != GOOGLE_PROVIDER:
        raise ConnectedAccountError(NOT_FOUND)
    return account


def _sign_out_locally(account_id: str, actor: Actor, *, db: sqlite3.Connection | None = None) -> tuple[int, str | None]:
    """Strip the refresh token, mark the row signed out and audit it, in one transaction."""
    with write_transaction(db, scope="google_sign_out") as conn:
        if _google_row(account_id, conn)["state"] not in SIGN_OUT_FROM:
            raise ConnectedAccountError(NOT_SIGNED_IN)
        taken = take_google_refresh_token(account_id, db=conn)
        set_state(account_id, "signed_out", db=conn)
        audit_id = record_audit(account_id, "sign_out", actor, db=conn)
    return audit_id, taken["refresh_token"] if taken else None


def _remove_locally(account_id: str, actor: Actor, *, db: sqlite3.Connection | None = None) -> tuple[int, str | None]:
    """Audit, then delete the bundle, reach, cursor, queued screens and row, in one transaction.

    The audit row keeps the account id after the row is gone: history tables carry no foreign key.
    """
    with write_transaction(db, scope="google_remove") as conn:
        account = _google_row(account_id, conn)
        held = resolve_google_credentials(account_id, db=conn)
        audit_id = record_audit(account_id, "remove", actor, db=conn)
        try:
            key_store.drop_managed_entry(str(account["credential_key"]), db=conn)
        except key_store.KeyStoreError:
            raise
        conn.execute("DELETE FROM connected_account_reach WHERE account_id=?", (account_id,))
        conn.execute("DELETE FROM email_screens WHERE account=?", (account_id,))
        conn.execute("DELETE FROM connected_accounts WHERE id=?", (account_id,))
    return audit_id, held["refresh_token"] if held else None


async def sign_out_google_account(account_id: str, *, actor: Actor) -> dict[str, object]:
    """Sign one account out: no usable credential stays; the row and its client stay for Sign in."""
    _check_actor(actor)
    audit_id, refresh_token = await run_in_db(_sign_out_locally, account_id, actor)
    cause = await _revoke(refresh_token)
    await run_in_db(set_audit_cause, audit_id, cause)
    return {"account_id": account_id, "state": "signed_out", "revocation": cause}


async def remove_google_account(account_id: str, *, actor: Actor) -> dict[str, object]:
    """Remove one account: its in-flight inbox check stops and nothing of it stays locally."""
    from events.connectors import gmail_poller

    _check_actor(actor)
    audit_id, refresh_token = await run_in_db(_remove_locally, account_id, actor)
    gmail_poller.cancel_account(account_id)
    cause = await _revoke(refresh_token)
    await run_in_db(set_audit_cause, audit_id, cause)
    return {"account_id": account_id, "removed": True, "revocation": cause}


def _widens(before: AppReach, after: AppReach) -> bool:
    if before.mode == "all":
        return False
    if after.mode == "all":
        return True
    return bool(after.project_ids - before.project_ids) or (after.outside_projects and not before.outside_projects)


def change_google_account_reach(
    account_id: str, reach: object, *, actor: Actor, db: sqlite3.Connection | None = None,
) -> dict[str, object]:
    """Save where one account works. Agents may only narrow it; widening needs the person."""
    _check_actor(actor)
    try:
        after = reach if isinstance(reach, AppReach) else normalize_reach(reach)
    except AppReachError:
        raise ConnectedAccountError(INVALID_REACH) from None
    if after == corsia_only_reach():
        raise ConnectedAccountError(EMPTY_REACH)
    with write_transaction(db, scope="google_reach") as conn:
        _google_row(account_id, conn)
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
