"""CORSIA's spend-past-plan-limit policy for harness connections (#2477 amendment 12).

``usage.allow_overage`` is off by default. While off, a fresh, unexpired plan
window at 100% makes its scope ``limited`` with reason
``plan_limit_overage_off``, even when the vendor still allows the call on
credits or extra usage. The fact is one more scoped entry under
``vendor_verdicts``; ``connection_availability()`` stays its only reader.
``connections.harness_usage`` remains the only writer: it calls
``derive_plan_limits`` inside its write transaction.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.stop_causes import PLAN_LIMIT_REASON
from connections.usage_verdicts import (
    ACCOUNT_SCOPE,
    effective_verdict,
    verdicts,
    window_scope,
)
from connections.usage_windows import parse_time, window_stale

#: The account's 'Spend past usage limit' switch in stored usage.
ALLOW_OVERAGE_KEY = "allow_overage"


def allows_overage(usage: object) -> bool:
    """True only when stored connection usage explicitly opts into spending past the plan limit."""
    return isinstance(usage, dict) and usage.get(ALLOW_OVERAGE_KEY) is True


def spends_past_plan(provider: str, usage: dict[str, Any], verdict: dict[str, Any]) -> bool:
    """Whether paid capacity covers a plan-exhaustion verdict: the one owner of that rule.

    True only with the account's switch on and the vendor's own facts saying paid capacity
    remains, read through its profile's ``paid_capacity`` hook. The stored verdict stays the
    vendor's; this only decides that it does not block.
    """
    if not allows_overage(usage):
        return False
    from connections.account_profiles import profile_for

    return profile_for(provider).paid_capacity(usage, verdict)


@dataclass(frozen=True, slots=True)
class Exhausted:
    """One scope's spent plan window: its binding reset (None: none stated) and newest reading."""

    resets_at: str | None
    read_at: datetime


def _fresh_readings(usage: dict[str, Any], now: datetime) -> Iterator[tuple[str, float, dict[str, Any]]]:
    for window in (usage.get("windows") or {}).values():
        if not isinstance(window, dict) or window_stale(window, now):
            continue
        key = window_scope(window.get("kind"), limit_id=window.get("limit_id"))
        percent = window.get("used_percent")
        if key is not None and not isinstance(percent, bool) and isinstance(percent, (int, float)):
            yield key, percent, window


def exhausted_scopes(usage: dict[str, Any], now: datetime) -> dict[str, Exhausted]:
    """Every scope with a fresh plan window at 100%, keyed by scope.

    A spent window with no stated reset makes the scope's reset unknown (decision
    208): no other window's reset can say when that one ends.
    """
    result: dict[str, Exhausted] = {}
    for key, percent, window in _fresh_readings(usage, now):
        if percent < 100:
            continue
        reset = parse_time(window.get("resets_at"))
        read_at = parse_time(window.get("used_percent_observed_at", window.get("observed_at"))) or now
        prior = result.get(key)
        if prior is not None:
            known = parse_time(prior.resets_at)
            reset = None if reset is None or known is None else max(reset, known)
            read_at = max(read_at, prior.read_at)
        result[key] = Exhausted(reset.isoformat() if reset else None, read_at)
    return result


def _allowed_since(verdict: object, read_at: datetime) -> bool:
    """Whether ``verdict`` is a vendor ``allowed`` observed after the spent reading."""
    observed = parse_time(verdict.get("observed_at")) if isinstance(verdict, dict) else None
    return isinstance(verdict, dict) and verdict.get("state") == "allowed" and observed is not None and observed > read_at


def _held(verdict: object, key: str, now: datetime) -> dict[str, Any] | None:
    if not isinstance(verdict, dict):
        return None
    model = None if key == ACCOUNT_SCOPE else key
    return effective_verdict({"vendor_verdicts": {key: verdict}}, model, now).blocking


def derive_plan_limits(
    usage: dict[str, Any], previous: object, now: datetime,
) -> list[tuple[str | None, dict[str, Any]]]:
    """Write the plan-exhausted verdicts into merged ``usage``; return each new one.

    A scope held by an active vendor limit keeps the vendor's reason. A vendor
    ``allowed`` (credits or extra usage) never lifts this policy's verdict while
    the window is still exhausted: the prior verdict is restored unchanged and
    is not reported as new, so one exhausted window stops each Turn once.

    A hold with no stated reset never expires, so it releases on newer evidence
    instead (decision 208): an ``allowed`` observed after the spent reading, or
    a fresh reading below 100%. Sign-in, model switch and switch-on release it
    through their own paths.
    """
    if allows_overage(usage):
        return []
    written: list[tuple[str | None, dict[str, Any]]] = []
    stored = verdicts(usage)
    prior = verdicts(previous) if isinstance(previous, dict) else {}
    changed = False
    exhausted = exhausted_scopes(usage, now)
    for key, fact in sorted(exhausted.items()):
        scope, resets_at = (None if key == ACCOUNT_SCOPE else key), fact.resets_at
        held = _held(stored.get(key), key, now)
        if held is not None and held.get("reason") != PLAN_LIMIT_REASON:
            continue
        if resets_at is None and _allowed_since(stored.get(key), fact.read_at):
            continue
        before = _held(prior.get(key), key, now)
        if (
            before is not None and before.get("reason") == PLAN_LIMIT_REASON
            and before.get("resets_at") == resets_at
        ):
            if stored.get(key) != prior[key]:
                stored[key], changed = prior[key], True
            continue
        verdict = {
            "state": "limited", "reason": PLAN_LIMIT_REASON, "resets_at": resets_at,
            "source": "plan_window", "scope_model": scope, "observed_at": now.isoformat(),
        }
        stored[key], changed = verdict, True
        written.append((scope, verdict))
    for key in sorted({key for key, percent, _ in _fresh_readings(usage, now) if percent < 100} - exhausted.keys()):
        held = _held(stored.get(key), key, now)
        if held is not None and held.get("reason") == PLAN_LIMIT_REASON and held.get("resets_at") is None:
            stored[key], changed = {
                "state": "allowed", "source": "plan_window",
                "scope_model": None if key == ACCOUNT_SCOPE else key, "observed_at": now.isoformat(),
            }, True
    if changed:
        usage["vendor_verdicts"] = stored
    return written


def plan_limited_scopes(usage: object, now: datetime) -> list[str | None]:
    """Scopes whose active verdict is this policy's, for release on opt-in."""
    if not isinstance(usage, dict):
        return []
    scopes = []
    for key, verdict in verdicts(usage).items():
        held = _held(verdict, key, now)
        if held is not None and held.get("reason") == PLAN_LIMIT_REASON:
            scopes.append(None if key == ACCOUNT_SCOPE else key)
    return scopes
