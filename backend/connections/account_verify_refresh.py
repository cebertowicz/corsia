"""Look up accounts and report model refresh outcomes during verification."""
from __future__ import annotations

from connections import harness_models
from connections.account_signin import failure_reference
from connections.vendor_accounts import AccountError, VENDORS, get_account
from infra.db_async import run_in_db


async def account_with_home(account_id):
    account = await run_in_db(get_account, account_id)
    if account["source_home"] is None:
        raise AccountError("account home is missing")
    if account["state"] == "removed":
        raise AccountError("account was removed; choose another permitted account")
    return account


async def refresh_models(account, receipt, binary, current_c6):
    if account["provider"] not in VENDORS or receipt is None or current_c6:
        return None, None, None
    try:
        result = await harness_models.refresh_provider(
            account["provider"], reason="account-verify", account_id=account["id"],
            expected_receipt=receipt, binary=binary,
        )
        if getattr(result, "cause", None) == "account_refresh_superseded":
            return None, "account_refresh_superseded", None
        if getattr(result, "status", None) == "ready":
            return True, None, None
        cause = getattr(result, "cause", None) or "account_refresh_failed"
    except Exception as exc:
        cause = type(exc).__name__
    ref = failure_reference(account["provider"], cause)
    return False, "account_refresh_failed", ref


async def signin_recheck(account_id, provider, verify):
    result = await verify(account_id)
    if result["vendor_signed_in"] is False:
        return None
    if result["model_refresh_cause"] == "account_refresh_superseded":
        return {"account_id": account_id, "route": None, "status": "superseded", "cause": "account_refresh_superseded"}
    if result["vendor_signed_in"] is None:
        return {"account_id": account_id, "route": None, "status": "unavailable", "cause": result["cause"],
                "ref": failure_reference(provider, result["cause"])}
    if result["model_refresh_ok"] is True:
        return {"account_id": account_id, "route": None, "status": "already_signed_in", "cause": None}
    if result["model_refresh_ok"] is False:
        return {"account_id": account_id, "route": None, "status": "model_refresh_failed",
                "cause": result["model_refresh_cause"], "ref": result["model_refresh_ref"]}
    return None
