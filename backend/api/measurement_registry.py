"""Canonical server-owned Measurement metric registry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from core.measurement_scope import Grain


@dataclass(frozen=True)
class MetricDefinition:
    """Describe one server-owned aggregate measurement series."""

    family: str
    unit: str
    minimum_n: int
    start: str
    proxy: bool = False
    companion: str | None = None
    available: bool = True


REGISTRY: dict[str, MetricDefinition] = {
    "outcomes": MetricDefinition("outcomes", "count", 0, "2026-08-10"),
    "first_try_acceptance": MetricDefinition("outcomes", "ratio", 20, "2026-09-14"),
    "rejections_per_outcome": MetricDefinition("outcomes", "ratio", 20, "2026-08-16"),
    "active_hours_per_outcome": MetricDefinition("active", "hours_per_outcome", 20, "2026-08-31", True),
    "outcomes_per_active_hour": MetricDefinition("active", "outcomes_per_hour", 20, "2026-08-31", True),
    "estimated_human_hours_per_active_hour": MetricDefinition("size", "estimated_human_hours_per_active_hour", 20, "2026-08-31", True),
    "time_on_platform_h": MetricDefinition("presence", "hours", 1, "2026-09-29"),
    "asks_per_outcome": MetricDefinition("decisions", "ratio", 20, "2026-09-07", True),
    "took_recommendation_pct": MetricDefinition("decisions", "ratio", 10, "2026-08-10", companion="answered_other_pct"),
    "answered_other_pct": MetricDefinition("decisions", "ratio", 10, "2026-08-10", companion="took_recommendation_pct"),
    "answered_neither_pct": MetricDefinition("decisions", "ratio", 10, "2026-08-10"),
    "fresh_tokens_per_outcome": MetricDefinition("tokens", "fresh_tokens_per_outcome", 20, "2026-08-10"),
    # A plain total, so one call in the period is enough to state it; the in-progress period is served too, flagged partial.
    "fresh_tokens": MetricDefinition("tokens", "fresh_tokens", 1, "2026-08-10"),
    "limit_hits": MetricDefinition("limits", "count", 0, "2026-08-10"),
    "dollar_trend": MetricDefinition("unavailable", "usd", 0, "2026-08-10", available=False),
}


# The stored UTC instant the token total counts as fully recorded from. A window that begins earlier was only
# partly recorded, so a change against it reads new, never a percentage over a partial base.
TOKENS_RECORDED_FROM = "2026-09-07"


def instrument_period(start: str, grain: Grain | None = None) -> str:
    """Return the period an instrument started in: its Monday, or its own day on the Days grain.

    A start dated mid-week still measured that week, and the served spine is made of
    completed periods: clipping at the raw date would retire the very first period the
    instrument observed, forever. Without a grain the period is the week.
    """
    day = date.fromisoformat(start[:10])
    return day.isoformat() if grain is not None and grain.kind == "days" else (day - timedelta(days=day.weekday())).isoformat()


def period_measured(metric: str, period: str, grain: Grain | None = None,
                    first_presence_period: str | None = None) -> bool:
    """Say whether one registry id's instrument existed in a period (a week, or a day on Days).

    Presence-backed metrics derive their observed range from the user's first recorded
    presence, so a request window cannot move the coverage boundary. Other metrics retain
    their registry-owned instrument boundary.
    """
    if REGISTRY[metric].family in {"active", "presence"}:
        return bool(first_presence_period and period >= first_presence_period)
    return not period or period >= instrument_period(REGISTRY[metric].start, grain)
