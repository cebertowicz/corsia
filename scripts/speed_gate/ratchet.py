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
#: Two runs of one tree may differ in steps by this share of the larger run, or by ``STEP_SLACK``, whichever
#: is more. Statements must match exactly. A route's step count carries a sampling phase from every statement
#: any connection ran before it, so it moves by a few samples between runs; on the costliest routes the
#: wiggle grows with the work (a fraction of a percent), and 1% stays far below a real regression (a route
#: ceiling is 500,000 steps and a rise must exceed 25%).
RUN_BAND = 0.01
#: The floor under that band, for small routes, where one or two samples are a large share. Over 9 runs of the
#: fixture the largest deviation was 300 (two routes); this is twice that.
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


@dataclass
class Disagreement:
    """One count that differs between two runs of the same tree, with the band it was judged against."""

    route: str
    metric: str  # "steps" | "statements" | "bytes" | "measured"
    first: int | None
    second: int | None
    band: int

    @property
    def within_band(self) -> bool:
        return self.first is not None and self.second is not None and abs(self.first - self.second) <= self.band


def run_band(metric: str, first: int, second: int) -> int:
    """How far apart two runs of one tree may be on ``metric``."""
    larger = max(first, second)
    if metric == "statements":
        return 0
    if metric == "steps":
        return max(STEP_SLACK, int(larger * RUN_BAND))
    return max(16, larger // 100)  # response bytes: a few digits on a route that reports the host's own readings


def compare_runs(first: dict, second: dict) -> list[Disagreement]:
    """Every count that differs between two runs of one tree (the determinism check), in or out of its band.

    Only a difference outside the band (``Disagreement.within_band`` false) is an instability; the rest is
    reported so the tolerance is visible. A route measured in only one run is always outside the band.
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
                found.append(Disagreement(route, metric, a[metric], b[metric], run_band(metric, a[metric], b[metric])))
    return found
