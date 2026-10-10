"""Task-chain dollars per hour of size-rated work, per agent, for the grain's two windows.

One task-grain pass, reading what the page already owns instead of a second
attribution: each accepted task's whole-chain usage and API-equivalent price come
from ``measurement_model_cost.query_model_costs`` (the chain in ``measurement_chain``,
Cost's admission and classification), and its hours from the size rating in
``measurement_size`` (class midpoints).

A task counts only when both halves are known, so a missing half is never read as
zero and the numerator and denominator cover the same tasks:

* it has a usable size rating;
* at least one of its calls is priced, and none is an API-priceable call with no
  price (local calls have no list price by design and add no dollars);
* no call of its chain failed to reach the ledger.

The read covers the grain's latest and previous windows only. Each (agent, period)
serves ``dollars``, ``hours`` and ``n``, the tasks counted; dividing and withholding a
thin figure is :func:`dollars_metric`.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from api.cost_identity import IdentityResolver
from api.measurement_model_cost import materialize_chain, query_model_costs, query_recording_gaps
from api.measurement_size import rated_tasks
from api.model_groups import cost_class
from core.measurement_scope import Grain

# The one id the Agents table serves that is not a registry series: the agent's whole-chain
# priced spend divided by the hours of its size-rated accepted tasks. It has the table's
# own floor and no series of its own.
DOLLARS_PER_HOUR = "dollars_per_work_hour"


def query_task_hour_cost(db, params: dict[str, Any]) -> dict[tuple[str, str], dict[str, float]]:
    """Return ``{(root_id, period): {dollars, hours, n}}`` for the grain's latest and previous windows.

    ``params['grain']`` is the one the page prepared (``prepare_outcome_periods(roots=True)``); only the
    size-rated tasks' chains are read, so the cost follows how many tasks carry a rating.
    """
    grain = params["grain"]
    rated = rated_tasks(db, [*grain.previous.periods, *grain.latest.periods])
    if not rated:
        return {}
    chain = materialize_chain(db, params, rated)
    try:
        gaps = query_recording_gaps(db, params, chain_rows=chain)
        cost_rows = query_model_costs(db, params, IdentityResolver(db), chain_rows=chain)
    finally:
        db.execute(f"DROP TABLE IF EXISTS {chain}")  # the pooled connection outlives the request
    dollars: dict[str, float] = defaultdict(float)
    outcomes: dict[str, set[str]] = defaultdict(set)
    for row in cost_rows:
        task_id = str(row["task_id"])
        if task_id not in rated:
            continue
        outcomes[task_id].add(str(row["outcome"]))
        if row["outcome"] == "priced" and row["dollars"] is not None:
            dollars[task_id] += float(row["dollars"])
    by_root: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"dollars": 0.0, "hours": 0.0, "n": 0})
    for task_id, (root_id, period, hours) in rated.items():
        if task_id in gaps or cost_class(outcomes.get(task_id, ())) != "priced":
            continue
        figures = by_root[(root_id, period)]
        figures["dollars"] += dollars[task_id]
        figures["hours"] += hours
        figures["n"] += 1
    return dict(by_root)


def _pooled(costs: dict[tuple[str, str], dict[str, float]], root_id: str, periods: list[str]) -> dict[str, float]:
    """One agent's dollars, hours and tasks summed over some periods."""
    total = {"dollars": 0.0, "hours": 0.0, "n": 0}
    for period in periods:
        for key, value in (costs.get((root_id, period)) or {}).items():
            total[key] += value
    return total


def _value(figures: dict[str, float], minimum_n: int) -> float | None:
    """Dollars per hour, or None under the floor or without hours."""
    if int(figures["n"]) < minimum_n or not figures["hours"]:
        return None
    return figures["dollars"] / figures["hours"]


def dollars_metric(costs: dict[tuple[str, str], dict[str, float]], root_id: str, grain: Grain, minimum_n: int,
                   ) -> dict[str, Any]:
    """The Agents table's dollars-per-hour cell: pooled dollars over pooled hours per window, withheld under the floor."""
    latest, previous = _pooled(costs, root_id, list(grain.latest.periods)), _pooled(costs, root_id, list(grain.previous.periods))
    return {
        "value": _value(latest, minimum_n), "n": int(latest["n"]), "minimum_n": minimum_n,
        "previous_value": _value(previous, minimum_n), "previous_n": int(previous["n"]),
        "periods": [{"period": period, "value": _value(figures, minimum_n), "n": int(figures["n"]), "minimum_n": minimum_n}
                  for period in grain.latest.periods
                  for figures in (_pooled(costs, root_id, [period]),)],
    }
