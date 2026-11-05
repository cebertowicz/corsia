"""Vendor sign-out and its durable cleanup receipt."""

import logging
from typing import Any

from connections.account_profiles import profile_for, sign_out_account
from connections.vendor_accounts import AUTH_CLEANUP_CAUSES, record_auth_cleanup
from infra.db_async import run_in_db
from infra.time import utc_now_iso

log = logging.getLogger(__name__)
_CLEANUP_STATUSES = ("done", "failed", "skipped", "pending")


async def vendor_logout(account: dict) -> dict[str, Any]:
    """Run the vendor's own logout for this account's stored home; nothing falls back to another home."""
    from connections.account_controls import _binary, _run

    provider = account["provider"]
    binary = await _binary(provider)
    if binary is None:
        return {"status": "failed", "cause": profile_for(provider).missing_cause}
    try:
        code, _ = await _run(provider, account["source_home"], binary, "logout")
    except (OSError, TimeoutError):
        return {"status": "failed", "cause": "vendor_logout_unavailable"}
    return {"status": "done", "cause": None} if code == 0 else {"status": "failed", "cause": "vendor_logout_failed"}


async def cleanup(account: dict) -> dict:
    """Run and record the same provider cleanup, also for a refused login."""
    account_id = account["id"]
    try:
        result = await sign_out_account(account)
        cleanup = {"status": result["status"], "cause": result.get("cause")}
        # Only a fixed name is stored or shown, never free text from a provider.
        named = isinstance(cleanup["cause"], (str, type(None))) and cleanup["cause"] in AUTH_CLEANUP_CAUSES
        if cleanup["status"] not in _CLEANUP_STATUSES or not named:
            cleanup = {"status": "failed", "cause": "sign_out_result_invalid"}
    except Exception as exc:  # noqa: BLE001 - a provider hook's failure must not undo the committed state
        log.warning("account %s sign-out hook failed: %s", account_id, type(exc).__name__)
        cleanup = {"status": "failed", "cause": "sign_out_hook_failed"}
    cleanup["at"] = utc_now_iso()
    await run_in_db(record_auth_cleanup, account_id, cleanup)
    return cleanup
