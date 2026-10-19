"""Local-only controls for Microsoft rows; Microsoft session revocation is deliberately never called."""
from __future__ import annotations

import sqlite3

from connections.connected_accounts import Actor, ConnectedAccountError, MICROSOFT_PROVIDER, _check_actor, get_account, record_audit, set_audit_cause
from connections.microsoft.oauth import clear_pending
from infra import key_store
from infra.database import write_transaction
from infra.db_async import run_in_db
from infra.time import utc_now_iso

NOT_FOUND = "connected account not found"
NOT_SIGNED_IN = "account is not signed in"


def _microsoft_row(account_id: str, conn: sqlite3.Connection) -> dict:
    account = get_account(account_id, db=conn)
    if account["provider"] != MICROSOFT_PROVIDER:
        raise ConnectedAccountError(NOT_FOUND)
    return account


def _sign_out_local(account_id: str, actor: Actor) -> int:
    with write_transaction(None, scope="microsoft_sign_out") as conn:
        account = _microsoft_row(account_id, conn)
        if account["state"] not in {"connected", "needs_sign_in"}:
            raise ConnectedAccountError(NOT_SIGNED_IN)
        key_store.drop_managed_entry(str(account["credential_key"]), db=conn)
        conn.execute("UPDATE connected_accounts SET state='signed_out',state_cause=NULL,updated_at=? WHERE id=?", (utc_now_iso(), account_id))
        return record_audit(account_id, "sign_out", actor, revocation_cause="manual_only", db=conn)


def _remove_local(account_id: str, actor: Actor) -> int:
    with write_transaction(None, scope="microsoft_remove") as conn:
        account = _microsoft_row(account_id, conn)
        audit_id = record_audit(account_id, "remove", actor, revocation_cause="manual_only", db=conn)
        key_store.drop_managed_entry(str(account["credential_key"]), db=conn)
        conn.execute("DELETE FROM connected_account_reach WHERE account_id=?", (account_id,))
        conn.execute("DELETE FROM email_screens WHERE account=?", (account_id,))
        conn.execute("DELETE FROM connected_accounts WHERE id=?", (account_id,))
    return audit_id


async def sign_out_microsoft_account(account_id: str, *, actor: Actor) -> dict[str, object]:
    _check_actor(actor)
    audit_id = await run_in_db(_sign_out_local, account_id, actor)
    await run_in_db(set_audit_cause, audit_id, "manual_only")
    return {"account_id": account_id, "state": "signed_out", "revocation": "manual_only"}


async def remove_microsoft_account(account_id: str, *, actor: Actor) -> dict[str, object]:
    _check_actor(actor)
    await clear_pending(account_id)
    audit_id = await run_in_db(_remove_local, account_id, actor)
    await run_in_db(set_audit_cause, audit_id, "manual_only")
    return {"account_id": account_id, "removed": True, "revocation": "manual_only"}
