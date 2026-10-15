"""Translate Claude Code usage frames into canonical harness usage observations.

``connections.harness_usage`` owns persistence and availability. This adapter
keeps Claude's account and scoped weekly windows intact and never derives a
second status in a catalog reader.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from connections import harness_usage
from connections.account_receipts import ReceiptFence
from connections.usage_store import claude_tier
from connections.usage_verdicts import ACCOUNT_WINDOWS, window_scope

PROVIDER = "claude-code"
_ALLOWED = ("allowed", "allowed_warning")


def _iso(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat() if value > 0 else None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).isoformat()


def _extra_usable(extra: object) -> bool | None:
    if not isinstance(extra, dict) or extra.get("is_enabled") is not True:
        return False
    utilization = extra.get("utilization")
    if isinstance(utilization, (int, float)) and not isinstance(utilization, bool):
        return utilization < 100
    used, limit = extra.get("used_credits"), extra.get("monthly_limit")
    if isinstance(used, (int, float)) and isinstance(limit, (int, float)):
        return used < limit
    return None


def _usage_windows(response: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    limits = response.get("rate_limits")
    if not isinstance(limits, dict):
        return [], {}
    windows = []
    for kind, raw in limits.items():
        if kind == "extra_usage" or not isinstance(raw, dict):
            continue
        percent = raw.get("utilization")
        if not isinstance(percent, (int, float)) or isinstance(percent, bool):
            continue
        windows.append({
            # A plan window's scope is the tier its kind names: the key an event writes too.
            "vendor": PROVIDER, "kind": kind,
            "scope_model": claude_tier(kind) if window_scope(kind) is not None else raw.get("scope_model"),
            "used_percent": percent, "resets_at": _iso(raw.get("resets_at")),
            "vendor_status": raw.get("status"), "source": "discovery",
        })
    extra = limits.get("extra_usage")
    return windows, {"extra_usage": extra} if isinstance(extra, dict) else {}


def usage_verdict(response: object) -> dict[str, Any]:
    """Classify ``get_usage`` without mistaking enabled overage for usable overage."""
    if not isinstance(response, dict):
        return {"verdict": "unknown", "error": "Claude Code returned no usage report"}
    limits = response.get("rate_limits")
    if response.get("rate_limits_available") is False:
        return {"verdict": "unknown", "error": "Claude Code reports plan usage unavailable for this account"}
    if not isinstance(limits, dict):
        return {"verdict": "unknown", "error": "Claude Code returned malformed plan usage"}
    windows = {
        name: limits[name] for name in ACCOUNT_WINDOWS
        if isinstance(limits.get(name), dict)
        and isinstance(limits[name].get("utilization"), (int, float))
        and not isinstance(limits[name].get("utilization"), bool)
    }
    if not windows:
        return {"verdict": "unknown", "error": "Claude Code reported no plan usage windows"}
    exhausted = [name for name, window in windows.items() if window["utilization"] >= 100]
    if not exhausted:
        return {"verdict": "allowed"}
    extra = _extra_usable(limits.get("extra_usage"))
    if extra is True:
        return {"verdict": "allowed"}
    if extra is None:
        return {"verdict": "unknown", "error": "Claude Code could not verify extra usage"}
    resets = [_iso(windows[name].get("resets_at")) for name in exhausted]
    return {
        "verdict": "limited", "reason": f"{exhausted[0]}_limit",
        "resets_at": max(reset for reset in resets if reset) if any(resets) else None,
    }


def apply_usage_response(response: object, *, receipt: ReceiptFence | None, db=None) -> dict[str, Any]:
    """Persist every readable ``get_usage`` meter and its one verdict on ``receipt``'s login."""
    verdict = usage_verdict(response)
    windows, facts = _usage_windows(response) if isinstance(response, dict) else ([], {})
    if verdict["verdict"] == "unknown":
        harness_usage.record_usage(provider=PROVIDER, windows=windows, facts=facts,
                                   check_error=verdict["error"], receipt=receipt, db=db)
    else:
        harness_usage.record_usage(provider=PROVIDER, windows=windows, facts=facts, verdict={
            "state": verdict["verdict"], "reason": verdict.get("reason"),
            "resets_at": verdict.get("resets_at"), "source": "discovery",
        }, receipt=receipt, db=db)
    return verdict


def apply_rate_limit_event(info: object, *, receipt: ReceiptFence | None, db=None) -> bool:
    """Persist a live Claude verdict on ``receipt``'s login; it wins an equal-time control-read tie."""
    if not isinstance(info, dict):
        return False
    status, kind = info.get("status"), info.get("rateLimitType")
    scope, tier = window_scope(kind), claude_tier(kind)
    # The plan's own meters are the unnamed account signal and every plan-window
    # kind. Anything else — a named meter that maps to no plan window, or a value
    # that cannot name one — is an observation only.
    unnamed = kind is None or (isinstance(kind, str) and not kind)
    plan_meter = scope is not None or unnamed
    # The event's utilization is a 0-1 fraction; get_usage windows report 0-100.
    fraction = info.get("utilization")
    numeric_fraction: float | None = (
        float(fraction)
        if isinstance(fraction, (int, float)) and not isinstance(fraction, bool)
        else None
    )
    has_utilization = numeric_fraction is not None
    used_percent = round(numeric_fraction * 100, 2) if numeric_fraction is not None else None
    windows = []
    if isinstance(kind, str):
        window: dict[str, Any] = {
            "vendor": PROVIDER, "kind": kind, "scope_model": tier,
            "used_percent": used_percent, "resets_at": _iso(info.get("resetsAt")),
            "vendor_status": status, "source": "rate_limit_event",
        }
        # Inferred from vendor status, not a reading: a rejected plan window, or
        # overage in use on it, is spent even when the event omits its percentage.
        if window["used_percent"] is None and scope is not None and (
            status == "rejected" or info.get("isUsingOverage") is True or info.get("overageInUse") is True
        ):
            window["used_percent"] = 100
        windows.append(window)
    if status in _ALLOWED or (status == "rejected" and info.get("overageStatus") in _ALLOWED):
        verdict: dict[str, Any] | None = (
            {"state": "allowed", "source": "rate_limit_event"} if plan_meter else None
        )
    elif status == "rejected" and scope is not None:
        verdict = {
            "state": "limited", "reason": f"{kind}_limit",
            "resets_at": _iso(info.get("resetsAt")), "source": "rate_limit_event",
            "scope_model": tier,
        }
    else:
        verdict = None
    if verdict is None:
        # A status on a meter outside the plan is an observation, never a verdict.
        if windows:
            harness_usage.record_usage(provider=PROVIDER, windows=windows, receipt=receipt, db=db)
        return scope is not None and not has_utilization
    harness_usage.record_usage(provider=PROVIDER, windows=windows, verdict=verdict, receipt=receipt, db=db)
    # The live event still updates availability, but it is not a meter reading.
    # The terminal owner schedules the existing bounded control read after this Turn.
    return scope is not None and not has_utilization
