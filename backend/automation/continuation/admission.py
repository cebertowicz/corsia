"""Pure bounded admission and deterministic fairness policy."""

from __future__ import annotations

import hashlib
import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from operator import attrgetter

from automation.continuation.triggers import Trigger


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


MAX_GLOBAL_INFLIGHT = _env_int("CORSIA_CONTINUATION_MAX_INFLIGHT", 4)
SCOPE_COST_CEILING_USD = _env_float(
    "CORSIA_CONTINUATION_SCOPE_COST_USD", 5.0
)
MAX_PER_SCOPE_PER_SWEEP = _env_int(
    "CORSIA_CONTINUATION_MAX_PER_SCOPE", 2
)
MAX_DEFERRALS = _env_int("CORSIA_CONTINUATION_MAX_DEFERRALS", 5)
DEFERRAL_BASE_SECONDS = _env_int(
    "CORSIA_CONTINUATION_DEFERRAL_BASE_SECONDS", 180
)
JITTER_RATIO = 0.10
RESTART_JITTER_SECONDS = _env_int("CORSIA_RESTART_JITTER_SECONDS", 30)


@dataclass(frozen=True)
class AdmissionSnapshot:
    """I/O-free inputs required by the admission policy."""

    global_in_flight: int
    scope_cost_usd: dict[str, float]
    least_recent_dispatch: dict[str, str]


@dataclass(frozen=True)
class AdmissionDecision:
    """One pure admission result."""

    action: str
    reason: str | None
    next_attempt_at: datetime | None = None


def fair_order(
    batch: list[Trigger],
    snapshot: AdmissionSnapshot,
) -> list[Trigger]:
    """Round-robin scopes from least-recently dispatched to most recent."""

    groups: dict[str, deque[Trigger]] = {}
    for trigger in batch:
        groups.setdefault(trigger.scope_id, deque()).append(trigger)
    for group in groups.values():
        ordered = sorted(group, key=attrgetter("created_at", "id"))
        group.clear()
        group.extend(ordered)
    scope_pairs = [
        (snapshot.least_recent_dispatch.get(scope_id, ""), scope_id)
        for scope_id in groups
    ]
    scope_pairs.sort()
    scopes = [scope_id for _last_dispatch, scope_id in scope_pairs]
    ordered_batch: list[Trigger] = []
    while any(groups[scope_id] for scope_id in scopes):
        for scope_id in scopes:
            if groups[scope_id]:
                ordered_batch.append(groups[scope_id].popleft())
    return ordered_batch


def decide(
    trigger: Trigger,
    snapshot: AdmissionSnapshot,
    now: datetime,
    *,
    admitted_by_scope: dict[str, int],
    admitted_total: int,
) -> AdmissionDecision:
    """Apply global, cost, and per-scope limits in first-match order."""

    reason = None
    if snapshot.global_in_flight + admitted_total >= MAX_GLOBAL_INFLIGHT:
        reason = "global_in_flight"
    elif (
        snapshot.scope_cost_usd.get(trigger.scope_id, 0.0)
        >= SCOPE_COST_CEILING_USD
    ):
        reason = "scope_cost_ceiling"
    elif (
        admitted_by_scope.get(trigger.scope_id, 0)
        >= MAX_PER_SCOPE_PER_SWEEP
    ):
        reason = "scope_fairness"
    if reason is None:
        return AdmissionDecision("admit", None)
    if trigger.deferrals >= MAX_DEFERRALS:
        return AdmissionDecision("stall", "deferral_exhausted")
    return AdmissionDecision(
        "defer",
        reason,
        deferred_until(trigger.id, trigger.deferrals + 1, now),
    )


def deferred_until(
    trigger_id: str,
    deferrals: int,
    now: datetime,
) -> datetime:
    """Return bounded exponential deferral with deterministic ±10% jitter."""

    delay = DEFERRAL_BASE_SECONDS * (2 ** max(0, deferrals - 1))
    digest = hashlib.sha256(trigger_id.encode("utf-8")).digest()
    fraction = int.from_bytes(digest[:8], "big") / ((1 << 64) - 1)
    jitter = (fraction * 2.0 - 1.0) * JITTER_RATIO
    return now + timedelta(seconds=delay * (1.0 + jitter))


def restart_jitter_seconds(thread_id: str, *, maximum: int) -> int:
    """Spread restart admission deterministically over a bounded window."""

    if maximum <= 0:
        return 0
    digest = hashlib.sha256(thread_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % (maximum + 1)
