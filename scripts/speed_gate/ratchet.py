"""The cost ratchet: compare one measurement against the committed baseline.

Only the counts the harness recorded enter a verdict (SQLite VM steps, statements
and response bytes). Wall time is carried in the measurement for people and is
never read here. A comparison across different SQLite versions is not a verdict:
step counts differ between releases, so it is reported as ``version_mismatch``
and fails loudly (rebaseline needed) rather than being compared.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .routes import FOCUS_MESSAGES_ROUTE

METRICS = ("steps", "statements", "bytes")
#: A route may not cost more than this share above its baseline, per metric...
GROWTH = 0.25
#: ...unless the rise is smaller than this absolute amount (counts are quantised, small routes jitter by a unit).
MIN_RISE = {"steps": 10_000, "statements": 5, "bytes": 16_384}
#: Fresh-route ceilings: a route above one is a would-fail unless grandfathered.
CEILINGS = {"steps": 500_000, "statements": 500, "bytes": 1_048_576}
#: The fixed Focus page measures 58,671 bytes with a production-shaped agent
#: thread. Keep a twofold margin for ordinary variation while making a seed
#: payload regression fail even though its SQLite walk still exceeds the step cap.
ROUTE_CEILINGS = {
    FOCUS_MESSAGES_ROUTE: {"bytes": 131_072},
}
#: Two runs of one tree must agree exactly on steps and statements (the SQLite build is pinned, one pooled
#: connection serves each lane and background work is drained), and on response bytes except on the routes
#: below. A route listed here reports the host's own live readings, so its payload changes by a few digits
#: between two runs of the same tree; the tolerance is that route's, named with its reason, never a global band.
BYTE_TOLERANCE: dict[str, tuple[int, str]] = {
    "/api/system": (8, "reports psutil memory and CPU readings, whose digits change between calls"),
    "/api/system/host-resources": (8, "reports psutil memory and CPU readings, whose digits change between calls"),
}


@dataclass
class Finding:
    route: str
    kind: str  # "growth" | "ceiling" | "error" | "improved" | "new" | "removed"
    metric: str
    before: int | None
    after: int | None
    detail: str

    @property
    def fails(self) -> bool:
        return self.kind in ("growth", "ceiling", "error")


@dataclass
class Verdict:
    status: str  # "pass" | "would_fail" | "version_mismatch"
    findings: list[Finding] = field(default_factory=list)
    message: str = ""

    @property
    def failing(self) -> list[Finding]:
        return [item for item in self.findings if item.fails]


def measured(result: dict) -> dict[str, dict]:
    """Routes whose fixture request succeeded; an error response guards no read and is not baselined."""
    return {route: cost for route, cost in result["routes"].items() if cost.get("status", 200) < 400}


def route_ceilings(route: str, ceilings: dict, custom: dict[str, dict]) -> dict:
    """Merge the global caps with any evidence-backed cap for one query shape."""
    return {**ceilings, **custom.get(route, {})}


def over_ceiling(cost: dict, ceilings: dict) -> list[str]:
    return [metric for metric in METRICS if cost.get(metric, 0) > ceilings[metric]]


def _grew(before: int, after: int, metric: str) -> bool:
    return after > before * (1 + GROWTH) and after - before >= MIN_RISE[metric]


def compare(current: dict, baseline: dict) -> Verdict:
    """Verdict of ``current`` (a harness result) against ``baseline`` (the committed file)."""
    if current.get("sqlite_version") != baseline.get("sqlite_version"):
        return Verdict(
            "version_mismatch",
            message=(f"REBASELINE NEEDED: the baseline was recorded on SQLite {baseline.get('sqlite_version')}, this run used "
                     f"{current.get('sqlite_version')}: step counts are not comparable, so there is no verdict and the run is not a pass. "
                     "Regenerate the baseline on the new version with the update-baseline command."))
    ceilings = baseline.get("ceilings", CEILINGS)
    custom_ceilings = baseline.get("route_ceilings", {})
    grandfathered = set(baseline.get("grandfathered", ()))
    recorded = baseline.get("routes", {})
    not_called = current.get("not_called", {})
    findings: list[Finding] = []
    for route, cost in sorted(measured(current).items()):
        before = recorded.get(route)
        route_ceiling = route_ceilings(route, ceilings, custom_ceilings)
        if before is None:
            kind = "new"
            for metric in over_ceiling(cost, route_ceiling):
                findings.append(Finding(route, "ceiling", metric, None, cost[metric],
                                        f"new route exceeds the {metric} ceiling of {route_ceiling[metric]:,}"))
            if not over_ceiling(cost, route_ceiling):
                findings.append(Finding(route, kind, "steps", None, cost["steps"], "new route within every ceiling"))
            continue
        for metric in METRICS:
            if _grew(before[metric], cost[metric], metric):
                findings.append(Finding(route, "growth", metric, before[metric], cost[metric],
                                        f"{metric} rose more than {GROWTH:.0%} over the baseline"))
            elif cost[metric] < before[metric] * (1 - GROWTH) and before[metric] - cost[metric] >= MIN_RISE[metric]:
                findings.append(Finding(route, "improved", metric, before[metric], cost[metric],
                                        f"{metric} fell more than {GROWTH:.0%}: lower the baseline"))
        for metric in over_ceiling(cost, route_ceiling):
            # Legacy routes over a global ceiling remain grandfathered. A custom
            # cap is deliberate fresh evidence for this query shape and always
            # applies, even if another metric (Focus steps) stays grandfathered.
            if route not in grandfathered or metric in custom_ceilings.get(route, {}):
                findings.append(Finding(route, "ceiling", metric, before[metric], cost[metric],
                                        f"{metric} is over the ceiling of {route_ceiling[metric]:,}"))
    for route in sorted(set(recorded) - set(measured(current))):
        status = current["routes"].get(route, {}).get("status")
        if status is not None:  # the route answered with an error: its read is no longer measured
            findings.append(Finding(route, "error", "status", recorded[route]["steps"], status,
                                    f"HTTP {status} from the fixture request; a baselined route must still answer"))
        elif route in not_called:  # still in the app, but the harness can no longer call it
            findings.append(Finding(route, "error", "status", recorded[route]["steps"], None,
                                    f"the gate can no longer call this baselined route: {not_called[route]}"))
        else:  # in neither list: the app has no such route any more
            findings.append(Finding(route, "removed", "steps", recorded[route]["steps"], None,
                                    "route is no longer in the application"))
    status = "would_fail" if any(item.fails for item in findings) else "pass"
    return Verdict(status, findings)


def new_baseline(current: dict, reason: str, ceilings: dict | None = None) -> dict:
    """A baseline recording every measured route; routes already over a ceiling are grandfathered."""
    ceilings = ceilings or CEILINGS
    routes = {route: {metric: cost[metric] for metric in METRICS} for route, cost in sorted(measured(current).items())}
    custom_ceilings = {route: ROUTE_CEILINGS[route] for route in routes if route in ROUTE_CEILINGS}
    return {
        "sqlite_version": current["sqlite_version"], "progress_every": current["progress_every"],
        "ceilings": ceilings, "route_ceilings": custom_ceilings, "reason": reason, "routes": routes,
        "grandfathered": [route for route, cost in routes.items() if over_ceiling(cost, ceilings)],
    }


def distribution(current: dict) -> list[tuple[str, dict]]:
    """Routes from most to least costly by steps, for the report."""
    return sorted(measured(current).items(), key=lambda item: -item[1]["steps"])


@dataclass
class Disagreement:
    """One count that differs between two runs of the same tree, with the tolerance it was judged against."""

    route: str
    metric: str  # "steps" | "statements" | "bytes" | "measured"
    first: int | None
    second: int | None
    tolerance: int  # 0 unless the route has a ``BYTE_TOLERANCE`` entry and the metric is bytes
    reason: str = ""

    @property
    def tolerated(self) -> bool:
        return self.first is not None and self.second is not None and abs(self.first - self.second) <= self.tolerance


def compare_runs(first: dict, second: dict) -> list[Disagreement]:
    """Every count that differs between two runs of one tree (the determinism check).

    Steps and statements must be equal. Bytes must be equal except on a route with a ``BYTE_TOLERANCE``
    entry, where a difference up to its tolerance is listed but ``tolerated``. A route measured in only one
    run is never tolerated.
    """
    found = []
    one, two = measured(first), measured(second)
    for route in sorted(set(one) | set(two)):
        a, b = one.get(route), two.get(route)
        if a is None or b is None:
            found.append(Disagreement(route, "measured", None if a is None else a["steps"],
                                      None if b is None else b["steps"], 0))
            continue
        for metric in METRICS:
            if a[metric] != b[metric]:
                tolerance, reason = BYTE_TOLERANCE.get(route, (0, "")) if metric == "bytes" else (0, "")
                found.append(Disagreement(route, metric, a[metric], b[metric], tolerance, reason))
    return found
