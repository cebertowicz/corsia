"""Canonical server-owned Measurement metric registry."""

from __future__ import annotations

from dataclasses import dataclass

from core.measurement_scope import Grain


@dataclass(frozen=True)
class MetricDefinition:
    """Describe one server-owned aggregate measurement series."""

    family: str
    unit: str
    minimum_n: int
    method_version: int = 1
    proxy: bool = False
    companion: str | None = None
    available: bool = True
    # Measured from the user's first recorded presence, not ``start``: presence is the only
    # instrument behind the series' coverage.
    presence_backed: bool = False


REGISTRY: dict[str, MetricDefinition] = {
    "outcomes": MetricDefinition("outcomes", "count", 0),
    "first_try_acceptance": MetricDefinition("outcomes", "ratio", 20),
    "rejections_per_outcome": MetricDefinition("outcomes", "ratio", 20),
    "active_hours_per_outcome": MetricDefinition("active", "hours_per_outcome", 20, proxy=True, presence_backed=True),
    "outcomes_per_active_hour": MetricDefinition("active", "outcomes_per_hour", 20, proxy=True, presence_backed=True),
    # Sized work is its own instrument: its rows and subtotals run from its start. Its rate divides by
    # presence time, so the rate (rows and windows) is measured only from the first presence period.
    "estimated_human_hours_per_active_hour": MetricDefinition("size", "estimated_human_hours_per_active_hour", 20, proxy=True),
    # A plain total: a measured period with no presence is a true zero, so nothing is withheld.
    "time_on_platform_h": MetricDefinition("presence", "hours", 0, presence_backed=True),
    "asks_per_outcome": MetricDefinition("decisions", "ratio", 20, proxy=True),
    "took_recommendation_pct": MetricDefinition("decisions", "ratio", 10, companion="answered_other_pct"),
    "answered_other_pct": MetricDefinition("decisions", "ratio", 10, companion="took_recommendation_pct"),
    "answered_neither_pct": MetricDefinition("decisions", "ratio", 10),
    "fresh_tokens_per_outcome": MetricDefinition("tokens", "fresh_tokens_per_outcome", 20),
    # A plain total, so one call in the period is enough to state it; the in-progress period is served too, flagged partial.
    "fresh_tokens": MetricDefinition("tokens", "fresh_tokens", 1),
    "limit_hits": MetricDefinition("limits", "count", 0),
    "dollar_trend": MetricDefinition("unavailable", "usd", 0, available=False),
}



def period_measured(metric: str, period: str, grain: Grain | None = None,
                    first_presence_period: str | None = None, source_start: str | None = None) -> bool:
    """Say whether one registry id's instrument existed in a period (a week, or a day on Days).

    Presence-backed metrics derive their observed range from the user's first recorded
    presence, so a request window cannot move the coverage boundary. Other metrics retain
    their registry-owned instrument boundary.
    """
    if REGISTRY[metric].presence_backed:
        return bool(first_presence_period and period >= first_presence_period)
    return bool(source_start and (not period or period >= source_start))
