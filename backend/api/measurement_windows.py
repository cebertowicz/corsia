"""The two comparison windows of a Measurement series, pooled from the periods' raw counts.

A window's figure is the series' own formula applied to the summed counts of its periods, so it
weighs each period by its own denominator (outcomes, active hours, rated tasks) and a quiet day
never counts as much as a busy one. It uses the same minimum-n rule as a period row; under the
minimum the value is withheld and the n is still reported, so the page can say how short it fell.
"""
from __future__ import annotations

from typing import Any, Callable

from api.measurement_size import pooled_size_window
from core.measurement_scope import Grain, Window


def _summed(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Every numeric count of the periods' source rows, added; a missing count adds nothing."""
    total: dict[str, Any] = {}
    for row in rows:
        for key, value in row.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total[key] = total.get(key, 0) + value
    return total


def window_figure(metric: str, window: Window, sources: dict[str, dict[str, Any]], *,
                  row: Callable[..., dict[str, Any]], measured: Callable[[str, str], bool],
                  periods: set[str] | None = None, size_minimum_n: int | None = None) -> dict[str, Any]:
    """One window's ``{value, n}``: the periods the instrument existed in, pooled.

    ``periods`` narrows the window to the periods the series itself serves (a series that
    starts at its first observed period has no earlier one to pool). ``size_minimum_n`` is
    set for the size-rated series, whose rate is estimated hours over active hours.
    """
    kept = [period for period in window.periods
            if measured(metric, period) and (periods is None or period in periods)]
    pooled = _summed([sources[period] for period in kept if period in sources])
    if size_minimum_n is not None:
        return pooled_size_window(pooled, bool(kept), size_minimum_n)
    served = row(metric, kept[-1] if kept else window.last, pooled if kept else {})
    return {"value": served["value"], "n": served["n"]}


def series_windows(metric: str, grain: Grain, sources: dict[str, dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    """The latest window and the one before it, for one served series id."""
    return {"latest": window_figure(metric, grain.latest, sources, **kwargs),
            "previous": window_figure(metric, grain.previous, sources, **kwargs)}
