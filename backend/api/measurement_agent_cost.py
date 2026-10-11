"""Task-chain dollars per hour of size-rated work, per agent, for the grain's two windows.

One task-grain pass, reading what the page already owns instead of a second
attribution: each accepted task's whole-chain usage and API-equivalent price come
from ``measurement_model_cost.query_model_costs`` (the chain in ``measurement_chain``,
Cost's admission and classification), and its hours from the size rating in
``measurement_size`` (class midpoints).

A task counts when it has a usable size rating and priced API-equivalent dollars
above zero in its chain; its other calls (local, unpriceable, or missing from the
ledger) are not in the figure, so the dollars are what could be priced.

The read covers the grain's latest and previous windows only. Each (agent, period)
serves ``dollars``, ``hours`` and ``n``, the tasks counted; dividing and withholding a
thin figure is :func:`dollars_metric`.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from api.measurement_size import rated_tasks
from core.measurement_scope import Grain

# The one id the Agents table serves that is not a registry series: the agent's whole-chain
# priced spend divided by the hours of its size-rated accepted tasks. It has the table's
# own floor and no series of its own.
DOLLARS_PER_HOUR = "dollars_per_work_hour"


def query_task_hour_cost(db, params: dict[str, Any], cost_rows: list[dict[str, Any]],
                         ) -> dict[tuple[str, str], dict[str, float]]:
    """Return ``{(root_id, period): {dollars, hours, n}}`` for the grain's latest and previous windows.

    ``params['grain']`` is the one the page prepared (``prepare_outcome_periods(roots=True)``). Only the
    size-rated tasks count. ``cost_rows`` are the classified rows from
    ``api.measurement_chain_costs.chain_costs`` for every accepted task in the same two windows; a task's rows
    are the same whichever tasks were walked with it.
    """
    grain = params["grain"]
    rated = rated_tasks(db, [*grain.previous.periods, *grain.latest.periods])
    if not rated:
        return {}
    dollars: dict[str, float] = defaultdict(float)
    for row in cost_rows:
        task_id = str(row["task_id"])
        if task_id not in rated:
            continue
        if row["outcome"] == "priced" and row["dollars"] is not None:
            dollars[task_id] += float(row["dollars"])
    by_root: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"dollars": 0.0, "hours": 0.0, "n": 0})
    for task_id, (root_id, period, hours) in rated.items():
        if dollars[task_id] <= 0:
            continue
        figures = by_root[(root_id, period)]
        figures["dollars"] += dollars[task_id]
        figures["hours"] += hours
        figures["n"] += 1
    return dict(by_root)


def _pooled(costs: dict[tuple[str, str], dict[str, float]], root_id: str | None, periods: list[str]) -> dict[str, float]:
    """One agent's dollars, hours and tasks summed over some periods; every agent's when ``root_id`` is None."""
    total = {"dollars": 0.0, "hours": 0.0, "n": 0}
    for (root, period), figures in costs.items():
        if period in periods and root_id in (None, root):
            for key, value in figures.items():
                total[key] += value
    return total


def _value(figures: dict[str, float], minimum_n: int) -> float | None:
    """Dollars per hour, or None under the floor or without hours."""
    if int(figures["n"]) < minimum_n or not figures["hours"]:
        return None
    return figures["dollars"] / figures["hours"]


def dollars_metric(costs: dict[tuple[str, str], dict[str, float]], root_id: str | None, grain: Grain, minimum_n: int,
                   ) -> dict[str, Any]:
    """The Agents table's dollars-per-hour cell: pooled dollars over pooled hours per window, withheld under the floor.

    ``root_id=None`` is the same cell for all the scope's agents pooled: the figure an agent's own is read against.
    """
    latest, previous = _pooled(costs, root_id, list(grain.latest.periods)), _pooled(costs, root_id, list(grain.previous.periods))
    return {
        "value": _value(latest, minimum_n), "n": int(latest["n"]), "minimum_n": minimum_n,
        "previous_value": _value(previous, minimum_n), "previous_n": int(previous["n"]),
        "periods": [{"period": period, "value": _value(figures, minimum_n), "n": int(figures["n"]), "minimum_n": minimum_n}
                  for period in grain.latest.periods
                  for figures in (_pooled(costs, root_id, [period]),)],
    }
