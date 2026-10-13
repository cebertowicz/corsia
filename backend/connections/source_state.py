"""The one state vocabulary for Claude Code and Codex, and where it is kept.

``ConnectionStatus`` stays the owner of whether a connector is usable. The last
check's own result (``state``, ``prior_state``, ``reason``, ``checked_at``) sits
in the connection row's config under ``source_check``.

An unverified result never demotes a row that was ready: that row keeps status
AVAILABLE, so dispatch keeps working, and reports ``unverified`` with
``prior_state="ready"``. Any other row becomes UNVERIFIED, which is not
available. Nothing here polls.

When a sign-out is noticed:

* Claude Code: whenever its catalog session runs (``claude_discovery``), which
  is at startup, on the recheck route, and on each catalog refresh the request
  paths start (a stale catalog, a new CLI version, a selector never seen). A
  failed session reads needs_auth only when ``claude auth status`` exits 1.
* Codex: only at startup and on the recheck route. ``codex login status`` runs
  at three sites: ``seed_builtin_connections`` and ``check_harness_source``
  (both at startup) and ``check_harness_source`` on the recheck route.
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
DISABLED = "disabled"
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
    ConnectionStatus.DISABLED.value: DISABLED,
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


def row_state(row: dict[str, Any] | None) -> dict[str, Any]:
    """``state``, ``prior_state``, ``reason`` and ``checked_at`` for a row."""
    if row is None:
        return {"state": NOT_INSTALLED, "prior_state": None, "reason": None, "checked_at": None}
    if row.get("status") == ConnectionStatus.DISABLED.value:
        return {"state": DISABLED, "prior_state": None, "reason": None, "checked_at": None}
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

    Codex runs ``codex login status``. Claude Code's answer is its catalog
    session's own: an available row is checked by the catalog refresh that runs
    beside this at startup, so only a row that is not available (or a recheck)
    runs that refresh here, and its receipt is the only result.
    """
    from connections import _codex_sign_in_state, resolve_builtin_binary
    from connections.harness_models import refresh_provider

    try:
        if rediscover:
            cfg, binary = await run_in_db(resolve_builtin_binary, provider)
            await run_in_db(_ensure_row, provider, cfg, binary)
        else:
            row = await run_in_db(get_connection, provider)
            if row is None or row["status"] in {
                ConnectionStatus.NOT_INSTALLED.value, ConnectionStatus.DISABLED.value,
            }:
                return
            if provider == "claude-code" and row["status"] == ConnectionStatus.AVAILABLE.value:
                return
            binary = (row.get("config") or {}).get("binary") or provider
    except Exception as exc:
        await run_in_db(apply_check, provider, UNVERIFIED, str(exc))
        return
    if binary is None:
        await run_in_db(apply_check, provider, NOT_INSTALLED)
    elif provider == "codex":
        state = await asyncio.to_thread(_codex_sign_in_state, binary)
        await run_in_db(apply_check, provider, state)  # its output is not kept
    else:
        receipt = await refresh_provider(provider, reason="manual", binary=binary)
        if not receipt.get("ok") and not receipt.get("source_check_recorded"):
            await run_in_db(apply_check, provider, UNVERIFIED, receipt.get("error"))
