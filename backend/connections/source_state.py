"""The one state vocabulary for Claude Code and Codex, and where it is kept.

``ConnectionStatus`` stays the owner of whether a connector is usable. The last
check's own result (``state``, ``prior_state``, ``reason``, ``checked_at``) sits
in the connection row's config under ``source_check``.

An unverified result never demotes a row that was ready: that row keeps status
AVAILABLE, so dispatch keeps working, and reports ``unverified`` with
``prior_state="ready"``. Any other row becomes UNVERIFIED, which is not
available. Nothing here polls.

Checks refresh the selected account's catalog through its account-owned home.
An unconfigured account reports that sign-in is needed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

from connections.models import (
    SOURCE_CHECK_KEY,
    ConnectionStatus,
    ConnectionType,
    get_connection,
    record_source_check,
    upsert_connection,
)
from infra.db_async import run_in_db

log = logging.getLogger(__name__)

READY = "ready"
NOT_INSTALLED = "not_installed"
NEEDS_AUTH = "needs_auth"
UNVERIFIED = "unverified"
HARNESS_PROVIDERS = ("claude-code", "codex")

_STATUS_OF = {
    READY: ConnectionStatus.AVAILABLE,
    NEEDS_AUTH: ConnectionStatus.NEEDS_AUTH,
    NOT_INSTALLED: ConnectionStatus.NOT_INSTALLED,
    UNVERIFIED: ConnectionStatus.UNVERIFIED,
}
_STATE_OF = {
    ConnectionStatus.AVAILABLE.value: READY,
    ConnectionStatus.NEEDS_AUTH.value: NEEDS_AUTH,
    ConnectionStatus.NOT_INSTALLED.value: NOT_INSTALLED,
    ConnectionStatus.UNVERIFIED.value: UNVERIFIED,
}
_REASON_LIMIT = 80
_INTERNAL_PREFIX = re.compile(r"^Claude Code [\w -]*? (?:failed|timed out): ?", re.I)


def _prior_state(existing: dict[str, Any] | None) -> str | None:
    """The last definite state of a row, looking through an unverified one."""
    if not existing:
        return None
    check = (existing.get("config") or {}).get(SOURCE_CHECK_KEY) or {}
    if check.get("state") == UNVERIFIED:
        return check.get("prior_state")
    if check.get("state"):
        return check["state"]
    # No recorded check yet (a row from before checks were kept): its status stands.
    state = _STATE_OF.get(existing.get("status"))
    return None if state == UNVERIFIED else state


def refresh_check(result: dict) -> tuple[str, str | None]:
    """The source state and raw detail one vendor refresh result proves.

    The internal ``account_discovery_failed`` cause is not one a person reads.
    """
    if result.get("ok"):
        return READY, None
    error = result.get("error")
    state = NEEDS_AUTH if error == "account_not_configured" else UNVERIFIED
    return state, None if error == "account_discovery_failed" else error


def user_reason(state: str, detail: str | None) -> str | None:
    """The reason a person reads, from a check's raw detail (which only the log keeps)."""
    if state == NEEDS_AUTH:
        return "Not signed in"
    if state != UNVERIFIED:
        return None
    if detail and "timed out" in detail.lower():
        return "Check timed out"
    lines = _INTERNAL_PREFIX.sub("", (detail or "").strip()).splitlines()
    text = lines[0].strip()[:_REASON_LIMIT].rstrip() if lines else ""
    return f"Check failed: {text}" if text else "Check failed"


def resolve_check(
    existing: dict[str, Any] | None, state: str, reason: str | None,
) -> tuple[ConnectionStatus, dict[str, Any]]:
    """The status and ``source_check`` a new result gives a connection row.

    ``reason`` is the raw detail: it is logged, and only its user-facing form is stored.
    """
    if reason:
        log.info("source check %s: %s", state, reason)
    prior = _prior_state(existing) if state == UNVERIFIED else None
    status = _STATUS_OF[state]
    if state == UNVERIFIED and prior == READY:
        status = ConnectionStatus.AVAILABLE  # still usable; the check says why not sure
    return status, {
        "state": state,
        "prior_state": prior,
        "reason": user_reason(state, reason),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def row_state(row: dict[str, Any] | None) -> dict[str, Any]:
    """``state``, ``prior_state``, ``reason`` and ``checked_at`` for a row."""
    if row is None:
        return {"state": NOT_INSTALLED, "prior_state": None, "reason": None, "checked_at": None}
    check = (row.get("config") or {}).get(SOURCE_CHECK_KEY)
    if check and check.get("state"):
        return {key: check.get(key) for key in
                ("state", "prior_state", "reason", "checked_at")}
    if row.get("status") == ConnectionStatus.AVAILABLE.value:
        # Usable, but no probe has ever confirmed it.
        return {"state": UNVERIFIED, "prior_state": None,
                "reason": "Not checked yet", "checked_at": None}
    return {"state": _STATE_OF.get(row.get("status"), UNVERIFIED), "prior_state": None,
            "reason": None, "checked_at": None}


def apply_check(
    provider: str, state: str, reason: str | None = None, *, db=None,
    schedule_usage_check: bool = True,
):
    """Record one check result against the stored row; the row must exist."""
    status, check = resolve_check(get_connection(provider, db=db), state, reason)
    row = record_source_check(provider, status=status, check=check, db=db,
                              schedule_usage_check=schedule_usage_check)
    if row is None:
        raise RuntimeError(f"{provider} source state was not recorded")
    return row


def record_last_account_left(provider: str, *, db) -> None:
    """Record that sign-in is needed once no signed-in account of the vendor remains.

    The writer the account lifecycle calls inside its own transaction, the counterpart
    of the READY an account's committed discovery records. A vendor that is not
    installed keeps that state.
    """
    from connections.vendor_accounts import list_accounts

    if any(account["state"] == "signed_in" for account in list_accounts(provider=provider, db=db)):
        return
    row = get_connection(provider, db=db)
    if row is None or row["status"] == ConnectionStatus.NOT_INSTALLED.value:
        return
    apply_check(provider, NEEDS_AUTH, "account_not_configured", db=db, schedule_usage_check=False)


def _ensure_row(provider: str, cfg: dict, binary: str | None) -> None:
    """Create or refresh the row so the check has a place to land."""
    existing = get_connection(provider)
    if existing and (existing.get("config") or {}).get("binary") == (binary or provider):
        return
    upsert_connection(
        type=ConnectionType.CLI, provider=provider,
        config={**cfg, "binary": binary or provider},
        status=existing["status"] if existing else ConnectionStatus.NOT_INSTALLED,
    )


async def check_harness_source(provider: str, *, rediscover: bool = False) -> None:
    """Re-check Claude Code / Codex; unrecordable outcomes raise, with no retry.

    Startup checks the stored binary and leaves a not-installed row alone. Only
    the recheck route (``rediscover``) looks for a binary again, so an install
    or a moved binary is picked up when the user asks.

    An available Claude Code row is checked by the catalog refresh beside this
    at startup. Other checks use the selected account's refresh result.
    """
    from connections import resolve_builtin_binary
    from connections.harness_models import refresh_provider

    try:
        if rediscover:
            cfg, binary = await run_in_db(resolve_builtin_binary, provider)
            await run_in_db(_ensure_row, provider, cfg, binary)
        else:
            row = await run_in_db(get_connection, provider)
            if row is None or row["status"] == ConnectionStatus.NOT_INSTALLED.value:
                return
            if provider == "claude-code" and row["status"] == ConnectionStatus.AVAILABLE.value:
                return
            binary = (row.get("config") or {}).get("binary") or provider
    except Exception as exc:
        await run_in_db(apply_check, provider, UNVERIFIED, str(exc))
        return
    if binary is None:
        await run_in_db(apply_check, provider, NOT_INSTALLED)
    else:
        receipt = await refresh_provider(provider, reason="manual", binary=binary)
        try:
            recorded = await run_in_db(apply_check, provider, *refresh_check(receipt))
            if recorded is None:
                raise RuntimeError(f"{provider} source state was not recorded")
        except Exception as exc:
            await run_in_db(apply_check, provider, UNVERIFIED, str(exc))
