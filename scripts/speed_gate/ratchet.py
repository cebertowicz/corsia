"""The cost ratchet: compare one measurement against the committed baseline.

Only the counts the harness recorded enter a verdict (SQLite VM steps, statements
and response bytes). Wall time is carried in the measurement for people and is
never read here. A comparison across different SQLite versions is not a verdict:
step counts differ between releases, so it is reported as ``version_mismatch``
and fails loudly (rebaseline needed) rather than being compared.
"""

from __future__ import annotations

from dataclasses import dataclass, field

METRICS = ("steps", "statements", "bytes")
#: A route may not cost more than this share above its baseline, per metric...
GROWTH = 0.25
#: ...unless the rise is smaller than this absolute amount (counts are quantised, small routes jitter by a unit).
MIN_RISE = {"steps": 10_000, "statements": 5, "bytes": 16_384}
#: Fresh-route ceilings: a route above one is a would-fail unless grandfathered.
CEILINGS = {"steps": 500_000, "statements": 500, "bytes": 1_048_576}
#: Two runs of one tree may differ by this many steps per route. Over 9 runs of the fixture the largest
#: deviation was 300 (two routes); this is twice that. Statements never moved and bytes moved by at most 1.
STEP_SLACK = 600


@dataclass
class Finding:
    route: str
    kind: str  # "growth" | "ceiling" | "improved" | "new" | "removed"
    metric: str
    before: int | None
    after: int | None
    detail: str

    @property
    def fails(self) -> bool:
        return self.kind in ("growth", "ceiling")


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
    grandfathered = set(baseline.get("grandfathered", ()))
    recorded = baseline.get("routes", {})
    findings: list[Finding] = []
    for route, cost in sorted(measured(current).items()):
        before = recorded.get(route)
        if before is None:
            kind = "new"
            for metric in over_ceiling(cost, ceilings):
                findings.append(Finding(route, "ceiling", metric, None, cost[metric],
                                        f"new route exceeds the {metric} ceiling of {ceilings[metric]:,}"))
            if not over_ceiling(cost, ceilings):
                findings.append(Finding(route, kind, "steps", None, cost["steps"], "new route within every ceiling"))
            continue
        for metric in METRICS:
            if _grew(before[metric], cost[metric], metric):
                findings.append(Finding(route, "growth", metric, before[metric], cost[metric],
                                        f"{metric} rose more than {GROWTH:.0%} over the baseline"))
            elif cost[metric] < before[metric] * (1 - GROWTH) and before[metric] - cost[metric] >= MIN_RISE[metric]:
                findings.append(Finding(route, "improved", metric, before[metric], cost[metric],
                                        f"{metric} fell more than {GROWTH:.0%}: lower the baseline"))
        if route not in grandfathered:
            for metric in over_ceiling(cost, ceilings):
                findings.append(Finding(route, "ceiling", metric, before[metric], cost[metric],
                                        f"{metric} is over the ceiling of {ceilings[metric]:,}"))
    for route in sorted(set(recorded) - set(measured(current))):
        findings.append(Finding(route, "removed", "steps", recorded[route]["steps"], None, "route no longer measured"))
    status = "would_fail" if any(item.fails for item in findings) else "pass"
    return Verdict(status, findings)


def new_baseline(current: dict, reason: str, ceilings: dict | None = None) -> dict:
    """A baseline recording every measured route; routes already over a ceiling are grandfathered."""
    ceilings = ceilings or CEILINGS
    routes = {route: {metric: cost[metric] for metric in METRICS} for route, cost in sorted(measured(current).items())}
    return {
        "sqlite_version": current["sqlite_version"], "progress_every": current["progress_every"],
        "ceilings": ceilings, "reason": reason, "routes": routes,
        "grandfathered": [route for route, cost in routes.items() if over_ceiling(cost, ceilings)],
    }


def distribution(current: dict) -> list[tuple[str, dict]]:
    """Routes from most to least costly by steps, for the report."""
    return sorted(measured(current).items(), key=lambda item: -item[1]["steps"])


def same_counts(first: dict, second: dict) -> list[str]:
    """Routes whose recorded counts differ between two runs (the determinism check).

    Statements must match exactly. Steps are sampled every ``progress_every`` instructions
    with the sample phase carried across statements and threads, so runs may differ by a few
    samples (``STEP_SLACK``); response bytes may differ by a few digits on a route that reports
    the host's own readings. Anything larger is a real difference between the runs.
    """
    slack = STEP_SLACK
    differing = []
    for route in sorted(set(measured(first)) | set(measured(second))):
        a, b = measured(first).get(route), measured(second).get(route)
        if a is None or b is None or a["statements"] != b["statements"] or abs(a["steps"] - b["steps"]) > slack:
            differing.append(route)
        elif abs(a["bytes"] - b["bytes"]) > max(16, a["bytes"] // 100):
            differing.append(route)
    return differing
