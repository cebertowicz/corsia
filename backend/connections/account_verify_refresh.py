"""Added-account guard and verification refresh reporting for sign-in controls."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from connections import harness_models
from connections.account_receipts import ReceiptFence
from connections.vendor_accounts import AccountError, VENDORS, get_account
from infra.db_async import run_in_db
from infra.failure_reference import new_reference

log = logging.getLogger(__name__)


async def custom_account(account_id: str) -> dict[str, Any]:
    account = await run_in_db(get_account, account_id)
    if account["source_home"] is None:
        raise AccountError("the Default account signs in through the vendor's own CLI")
    if account["state"] == "removed":
        raise AccountError("account was removed; choose another permitted account")
    return account


async def signin_recheck(
    account_id: str, verify: Callable[[str], Awaitable[dict[str, Any]]],
) -> dict[str, Any] | None:
    result = await verify(account_id)
    if result["vendor_signed_in"] is False or (
        result["vendor_signed_in"] is True and result["model_refresh_ok"] is None
    ):
        return None
    if result["vendor_signed_in"] is None:
        return {"account_id": account_id, "route": None, "status": "unavailable", "cause": result["cause"]}
    if result["model_refresh_ok"] is True:
        return {"account_id": account_id, "route": None, "status": "already_signed_in", "cause": None}
    return {"account_id": account_id, "route": None, "status": "model_refresh_failed",
            "cause": result["model_refresh_cause"], "ref": result["model_refresh_ref"]}


async def refresh_models(
    account: dict[str, Any], receipt: ReceiptFence | None, binary: str | None, current_c6: bool,
) -> tuple[bool | None, str | None, str | None]:
    provider, account_id = account["provider"], account["id"]
    if provider not in VENDORS or receipt is None or current_c6:
        return None, None, None
    try:
        result = await harness_models.refresh_provider(
            provider, reason="account-verify", account_id=account_id,
            expected_receipt=receipt, binary=binary,
        )
        if getattr(result, "status", None) == "ready":
            return True, None, None
        log_cause = getattr(result, "cause", None) or "account_refresh_failed"
    except Exception as exc:
        log_cause = type(exc).__name__
    ref = new_reference("signin")
    log.warning("account model refresh failed reference=%s cause=%s", ref, log_cause)
    return False, "account_refresh_failed", ref
