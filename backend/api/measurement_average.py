"""The moving average drawn over a trend chart: the measure pooled over the k periods ending at each period.

It is the series' own figure over the trailing span (7 days, or 4 weeks), the one the comparison
windows serve, so it weighs each period by its own denominator. It sits on the chart's own axis, a
per-period figure: a rate is pooled over the span, a count is the span's total over k. A span that
reaches before the instrument existed or before the first drawn period, or that includes the period still in progress, has no average.
"""
from __future__ import annotations

from typing import Any

from api.measurement_windows import window_figure
from core.measurement_scope import Grain, Window

# Series whose pooled span figure is a total, not a rate: the chart draws it as a per-period mean.
TOTALS = frozenset({"outcomes", "fresh_tokens", "limit_hits", "time_on_platform_h"})


def span_ending(grain: Grain, period: str) -> Window | None:
    """The k completed periods ending at ``period``, or None for the period in progress and for a span that starts before the drawn range."""
    if period not in grain.periods:
        return None
    size, index = grain.average_periods, grain.periods.index(period)
    if index < size - 1:
        return None
    keys = grain.periods[index - size + 1:index + 1]
    if keys[0] not in grain.served:
        return None
    return Window(keys[0], keys[-1], keys)


def series_average(metric: str, grain: Grain, period: str, eligible: set[str], sources: dict[str, dict[str, Any]],
                   **window_kwargs: Any) -> float | None:
    """One period's moving average of a served series, or None when its span is not wholly measured.

    ``eligible`` is every period the series is measured in, lead-in included; ``window_kwargs`` are
    the ones the comparison windows take, so the figure and its minimum-n rule are the windows' own.
    """
    span = span_ending(grain, period)
    if span is None or any(key not in eligible for key in span.periods):
        return None
    value = window_figure(metric, span, sources, periods=eligible, **window_kwargs)["value"]
    if value is None:
        return None
    return value / grain.average_periods if metric in TOTALS else value


def cost_average(series: list[dict[str, Any]], grain: Grain, period: str, minimum_sized: int) -> float | None:
    """The all-in cost per hour of work over the span ending at ``period``: priced spend over size-rated hours.

    Withheld, never zero, when the span begins before the cost series does or fewer than ``minimum_sized``
    outcomes were rated in it, the rule the comparison windows use.
    """
    span = span_ending(grain, period)
    if span is None or not series or span.first < series[0]["period"]:
        return None
    rows = [row for row in series if row["period"] in span.periods]
    priced = [float(row["dollars"]) for row in rows if row["dollars"] is not None]
    hours, sized = sum(float(row["hours"]) for row in rows), sum(int(row["sized"]) for row in rows)
    if not priced or not hours or sized < minimum_sized:
        return None
    return round(sum(priced) / hours, 10)


def add_averages(rows: list[dict[str, Any]], metric: str, grain: Grain, eligible: set[str],
                 sources: dict[str, dict[str, Any]], **window_kwargs: Any) -> None:
    """Give each served row of one series its ``average``; the period in progress has none."""
    for row in rows:
        row["average"] = None if row["period"] == grain.current else series_average(metric, grain, row["period"], eligible, sources, **window_kwargs)
