"""The one state vocabulary for model sources, and where each fact lives.

Claude Code and Codex: the connection row keeps only whether the CLI is
installed and its ``binary``. Whether the vendor is signed in and whether its
models were found live on its CORSIA accounts (``vendor_accounts`` and each
account's discovery record); ``harness_state`` derives ``state``,
``prior_state``, ``reason`` and ``checked_at`` from them on every read, and
nothing writes a copy of that state back to the row.

GitHub keeps its last check in its connection row's config under
``source_check`` (``resolve_check``). An unverified result never demotes a row
that was ready: that row keeps status AVAILABLE and reports ``unverified`` with
``prior_state="ready"``. Nothing here polls.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from connections.models import (
    SOURCE_CHECK_KEY,
    ConnectionStatus,
    ConnectionType,
    get_connection,
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


def harness_state(provider: str, *, db=None) -> dict[str, Any]:
    """Claude Code's or Codex's ``state``, ``prior_state``, ``reason`` and ``checked_at``.

    Not installed comes from the connection row; with no signed-in account it is
    ``needs_auth``. Any signed-in account whose current login has a ready discovery
    makes it ``ready``; when every such discovery's last refresh failed it keeps
    dispatching as ``unverified`` with ``prior_state="ready"``. Otherwise it is
    ``unverified``: "Not checked yet" until an account's discovery records a
    failure, then that failure's reason. ``checked_at`` is the latest account check.
    """
    from connections.account_evidence import current_ready_discovery, discovery_view
    from connections.vendor_accounts import list_accounts

    row = get_connection(provider, db=db)
    if row is None or row["status"] == ConnectionStatus.NOT_INSTALLED.value:
        return {"state": NOT_INSTALLED, "prior_state": None, "reason": None, "checked_at": None}
    accounts = [account for account in list_accounts(provider=provider, db=db) if account["state"] == "signed_in"]
    if not accounts:
        return {"state": NEEDS_AUTH, "prior_state": None, "reason": user_reason(NEEDS_AUTH, None),
                "checked_at": None}
    ready = [found for found in map(current_ready_discovery, accounts) if found]
    failures = [found["refresh_failure"] for found in ready if found.get("refresh_failure")]
    checks = [discovery_view(account)["discovery"] for account in accounts]
    checked_at = max((check.get("observed_at") or "" for check in checks + failures), default="") or None
    if len(failures) < len(ready):
        return {"state": READY, "prior_state": None, "reason": None, "checked_at": checked_at}
    failed = next(iter(failures), None) or next((check for check in checks if check.get("status") == "failed"), None)
    return {"state": UNVERIFIED, "prior_state": READY if failures else None,
            "reason": (failed.get("reason") or user_reason(UNVERIFIED, None)) if failed else "Not checked yet",
            "checked_at": checked_at}


def _record_install(provider: str, cfg: dict, binary: str | None) -> None:
    """Keep the CLI's binary and whether it is installed: all the row holds for this source."""
    upsert_connection(
        type=ConnectionType.CLI, provider=provider, config={**cfg, "binary": binary or provider},
        status=ConnectionStatus.AVAILABLE if binary else ConnectionStatus.NOT_INSTALLED,
    )


async def check_harness_source(provider: str) -> None:
    """Re-check Claude Code / Codex: find its binary again, then refresh its accounts.

    The binary and whether it is installed land on the connection row, so an
    install or a moved binary is picked up when the user asks. Each signed-in
    account's refresh records its own discovery, which ``harness_state`` reads.
    A failure raises.
    """
    from connections import resolve_builtin_binary
    from connections.harness_models import refresh_provider

    cfg, binary = await run_in_db(resolve_builtin_binary, provider)
    await run_in_db(_record_install, provider, cfg, binary)
    if binary is not None:
        await refresh_provider(provider, reason="manual", binary=binary)


