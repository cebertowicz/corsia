"""The automatic findings the Agents surface reports, and what they say about the scope.

Two claims live here and nowhere else: which own-baseline moves are adverse enough to
report (``_findings``, capped and severity-ordered), and what the whole scope's finding
result is (``derive_findings_state``). The metric rows come from
:mod:`api.measurement_agents`, which owns the derivation; nothing here re-declares an id,
a threshold or a date.

The all-eligible rule in :func:`derive_findings_state` is the approved conservative
contract, not this module's invention -- see
``shared/self-improvement/DRAFT-MEASUREMENT-ENGINEERING.md`` ("Flag rule and Needs a
look") and ``shared/self-improvement/DRAFT-MEASUREMENT-DASHBOARD.md`` ("Thin data").
"""

from __future__ import annotations

from typing import Any

# The two ids that may raise an automatic finding on their own; both are ids the
# registry already serves the panels, so a finding and the panel cannot disagree.
FINDING_METRICS = ("fresh_tokens_per_outcome", "first_try_acceptance")
# An automatic finding is a claim about one metric, so its action is fixed here
# rather than derived from whatever column the reader selected.
FINDING_ACTIONS = {
    "fresh_tokens_per_outcome": "Review its 5 costliest recent tasks.",
    "first_try_acceptance": "Review rejected/re-attempted tasks.",
}

FINDINGS_STATE_FLAGGED = "flagged"
FINDINGS_STATE_TOO_LITTLE_HISTORY = "too_little_history"
FINDINGS_STATE_HEALTHY = "healthy"
FINDINGS_STATE_NO_AGENTS = "no_agents"


def _severity_key(size: float | None, relative: float | None) -> tuple[int, float]:
    """Rank a finite standardized move first, then a defined relative move, then neither.

    A flat baseline makes "how many deviations" unbounded, and JSON has no finite
    spelling of infinity: the size stays null and the move is reported against the
    baseline mean instead. A non-compliant float once took the whole response down.
    """
    if size is not None:
        return 0, -float(size)
    if relative is not None:
        return 1, -float(relative)
    return 2, 0.0


def _findings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select at most five adverse own-baseline findings with their prescribed action."""
    findings: list[dict[str, Any]] = []
    for row in rows:
        for metric, rule in row["flags"].items():
            if not rule["eligible"] or not rule["flagged"]:
                continue
            findings.append({
                "agent": row["agent"], "metric": metric, "current": rule["current"],
                "mean": rule["mean"], "sd": rule["sd"], "n": row["n"],
                "standardized_size": rule["adverse_size"],
                "zero_variance_baseline": rule["zero_variance_baseline"],
                "relative_adverse_change": rule["relative_adverse_change"],
                "minimum_n": rule["minimum_n"], "window": rule["window"], "action": FINDING_ACTIONS[metric],
            })
    findings.sort(key=lambda item: (_severity_key(item["standardized_size"], item["relative_adverse_change"]),
                                    -item["n"], item["agent"]["name"], item["agent"]["id"]))
    return findings[:5]


def derive_findings_state(rows: list[dict[str, Any]], findings: list[dict[str, Any]], agent_count: int) -> str:
    """State the automatic finding result, and never call an empty scope healthy.

    Findings come first. A scope with no active agents is its own answer: nothing
    was compared, so neither ``healthy`` nor ``too_little_history`` describes it,
    and reusing the second would let the surface read as "no agent needs a look"
    over a scope that has no agents at all. For a nonempty population, every
    automatic finding metric of every agent must have been comparable, or the
    honest answer is too little history.

    This never reads the table's selected metric. Changing which column the reader
    is sorting by cannot change what the automatic findings found.

    The all-eligible rule is the approved conservative contract: a scope is only
    called healthy when every agent in it was actually compared. A quiet agent, or
    one whose usage rows were never recorded, therefore holds back the clean verdict
    rather than letting the surface claim nothing moved when part of the population
    was never measured. That is a known limit of the current product states, not a
    state to be redefined in this slice.
    """
    if findings:
        return FINDINGS_STATE_FLAGGED
    if agent_count == 0:
        return FINDINGS_STATE_NO_AGENTS
    if any(not rule["eligible"] for row in rows for rule in row["flags"].values()):
        return FINDINGS_STATE_TOO_LITTLE_HISTORY
    return FINDINGS_STATE_HEALTHY
