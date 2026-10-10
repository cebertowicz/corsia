"""Pure model projection of canonical plan usage; the caller supplies the clock."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from connections.overage_policy import allows_overage
from connections.usage_store import empty_usage as _empty_usage
from connections.usage_verdicts import ACCOUNT_SCOPE, effective_verdict, scope_matches, window_scope
from connections.usage_windows import window_stale


def _plan_windows(stored: dict[str, Any], model: str | None, current: datetime, *, every_scope: bool = False) -> list:
    """Plan windows tagged with their model scope; ``every_scope`` keeps tier windows no model names."""
    windows = []
    for window in (stored.get("windows") or {}).values():
        if not isinstance(window, dict):
            continue
        scope = window_scope(window.get("kind"), limit_id=window.get("limit_id"))
        if scope is None or not (every_scope or scope_matches(model, scope)):
            continue
        windows.append({
            **window, "scope_model": None if scope == ACCOUNT_SCOPE else scope,
            "state": "unknown" if window_stale(window, current) else "reported",
        })
    return windows


def account_usage(
    usage: dict[str, Any] | None, *, now: datetime, provider: str | None = None,
) -> dict[str, Any]:
    """One account's display usage: every plan window with its model scope, and its account-wide verdict.

    Whether a model can run on the account is decided per model by
    ``llm.account_catalog.account_model_status``; this is for showing usage only.
    """
    stored: dict[str, Any] = usage if isinstance(usage, dict) else _empty_usage()
    return project_for_model(stored, model=None, now=now, provider=provider) | {
        "windows": _plan_windows(stored, None, now, every_scope=True),
    }


def project_for_model(
    usage: dict[str, Any] | None, *, model: str | None = None, now: datetime, provider: str | None = None,
) -> dict[str, Any]:
    """Return the single catalog projection, plan windows only; unknown is visible but usable."""
    current = now
    stored: dict[str, Any] = usage if isinstance(usage, dict) else _empty_usage()
    windows = _plan_windows(stored, model, current)
    effective = effective_verdict(stored, model, current, provider)
    active = effective.blocking
    verdict: dict[str, Any] = effective.latest or {}
    if active is not None:
        state, usable, reason, reset = "limited", False, active.get("reason"), active.get("resets_at")
    else:
        state = str(verdict.get("state") or "unknown")
        if state == "limited":
            state = "unknown"  # An expired limit proves nothing about current usage.
        usable, reason, reset = True, None, None
        if effective.spending:
            state = "allowed"  # Plan exhausted, still usable: the account spends past its plan limit.
    return {
        "state": state, "usable": usable, "reason": reason, "resets_at": reset,
        "observed_at": verdict.get("observed_at"), "windows": windows,
        "facts": stored.get("facts") or {}, "check": stored.get("last_check"),
        "allow_overage": allows_overage(stored),
    }
