"""Identify unreferenced added accounts that have never completed a login."""
import logging

from connections.account_evidence import discovery_record
from connections.account_lifecycle import change_state
from connections.vendor_accounts import VENDORS, get_account, list_accounts
from infra.database import write_transaction
from infra.db_async import run_in_db

log = logging.getLogger(__name__)


def _referenced(account_id: str, *, db) -> bool:
    # A saved order contains every account after a reorder; disabled is an explicit choice.
    if db.execute("SELECT 1 FROM provider_policies, json_each(disabled_json) WHERE json_each.value=? LIMIT 1", (account_id,)).fetchone():
        return True
    checks = (
        ("thread_vendor_accounts", "account_id"),
        ("thread_vendor_accounts", "pending_account_id"),
        ("thread_vendor_account_holds", "account_id"),
        ("vendor_account_billing_revisions", "account_id"),
        ("vendor_account_billing_facts", "account_id"),
        ("active_turns", "vendor_account_id"),
    )
    return any(db.execute(f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1", (account_id,)).fetchone()
               for table, column in checks)


def commit_if_leftover(account_id: str, *, db=None) -> dict | None:
    """Check references and commit removal together, through the normal state owner."""
    with write_transaction(db, scope="vendor_account_leftover") as connection:
        account = get_account(account_id, db=connection)
        if (account["provider"] not in VENDORS or account["is_default"] or account["state"] != "signed_out"
                or discovery_record(account)["login_seq"] != 0 or account["identity"]
                or (account["auth_cleanup"] or {}).get("status") == "failed"
                or _referenced(account_id, db=connection)):
            return None
        return change_state(account_id, "removed", expected_revision=account["revision"], db=connection)


async def remove_if_leftover(account_id: str, *, cleaned: bool = False) -> None:
    from connections.account_cleanup import cleanup

    account = await run_in_db(commit_if_leftover, account_id)
    if account is not None and not cleaned:
        await cleanup(account)


async def settle_signin(operation) -> None:
    """Record a failed sign-in and remove a safely cleaned duplicate."""
    from connections.account_signin import failure_reference

    if operation.status == "failed" and operation.ref is None:
        operation.ref = failure_reference(operation.provider, operation.cause)
    if operation.status == "failed" and operation.cause == "same_account":
        await remove_if_leftover(operation.account_id, cleaned=True)


async def record_failed_stop(operation) -> None:
    from connections.vendor_accounts import record_auth_cleanup
    from infra.time import utc_now_iso

    operation.stop_failed = True
    await run_in_db(record_auth_cleanup, operation.account_id,
                    {"status": "failed", "cause": "signin_stop_failed", "at": utc_now_iso()})


async def remove_startup_leftovers() -> None:
    """Sign-ins do not survive restart; an added row created by API alone is treated alike.

    One account's failure keeps that row and never stops startup.
    """
    for account in await run_in_db(list_accounts):
        if account["provider"] in VENDORS:
            try:
                await remove_if_leftover(account["id"])
            except Exception as exc:  # noqa: BLE001 - a leftover row must not keep the backend from starting
                log.warning("startup leftover check for %s failed: %s", account["id"], type(exc).__name__)
