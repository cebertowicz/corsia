"""Selection and coverage helpers shared by Measurement series readers."""

from __future__ import annotations

from fastapi import HTTPException

from api.measurement_registry import REGISTRY, period_measured
from core.measurement_scope import Grain


def requested_series(ids: str) -> list[str]:
    """Return requested ids plus their registry-defined companions."""
    requested = list(dict.fromkeys(part for part in ids.split(",") if part))
    if not requested or any(metric not in REGISTRY or not REGISTRY[metric].available for metric in requested):
        raise HTTPException(400, "unknown or unavailable series id")
    for metric in tuple(requested):
        companion = REGISTRY[metric].companion
        if companion and companion not in requested:
            requested.append(companion)
    return requested


def metric_spine(metric: str, spine: list[str], first_limit_period: str | None,
                 first_presence_period: str | None, source_start: str | None,
                 grain: Grain) -> list[str]:
    """Return only periods the metric can describe for this scope."""
    if metric == "limit_hits":
        first = max(first_limit_period, source_start) if first_limit_period and source_start else first_limit_period or source_start
        return [period for period in spine if first and period >= first]
    return [period for period in spine if period_measured(metric, period, grain, first_presence_period, source_start)]
