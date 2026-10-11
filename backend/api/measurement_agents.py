"""Purpose-named Measurement Agents and findings reads.

The Agents surface serves ids the Measurement registry already owns for the whole
scope, at the grain of the agent that owns the plan instead. Nothing here
re-declares a metric: ids, denominators and the ``minimum_n`` that suppresses a
thin value come from the caller's ``registry``, and every aggregate comes from
the prepared grains in :mod:`api.measurement_queries`. A second vocabulary or a
second settlement pass is the failure mode this module exists to avoid.

Two floors are this surface's own, and are named as such:

* ``RANKING_MINIMUM_N`` — how many completed outcomes an agent needs in the
  page grain's latest window before the table ranks it or compares its periods at all.
* The per-metric ``minimum_n`` from the registry, which still decides whether one
  column's value is measured or withheld, exactly as it does on the panels.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
import json
from math import sqrt
from typing import Any

from api.measurement_queries import (
    PREPARED_ROOT_DIFFICULTIES,
    PREPARED_ROOTS,
    query_family,
)
from api.measurement_agent_attention import query_root_active
from api.measurement_agents_projects import project_summaries as _project_summaries
from api.measurement_agent_cost import DOLLARS_PER_HOUR, dollars_metric, query_task_hour_cost
from api.measurement_models import two_windows
from api.measurement_findings import FINDING_METRICS, _findings, _severity_key
from api.measurement_scope import query_params
from core.measurement_scope import Grain

# The automatic findings compare the newest completed period with this many before it,
# whatever the grain: the flag rule counts points, not a calendar span.
BASELINE_PERIODS = 8
FRAME_PERIODS = BASELINE_PERIODS + 1
RANKING_MINIMUM_N = 20

# The ids the Agents table serves, all ids the registry already serves the panels, so a column and its panel cannot differ.
TABLE_METRICS = ("outcomes", "fresh_tokens_per_outcome", "first_try_acceptance", "asks_per_outcome", "active_hours_per_outcome")
AGENTS_SORTS = ("ranked", "worsening")
# Ranking direction for each served id: more outcomes and more first-try
# acceptance are better; fewer tokens and fewer asks per outcome are better.
HIGHER_IS_BETTER = frozenset({"outcomes", "first_try_acceptance"})


def _minimum_n(registry: dict[str, Any], metric: str) -> int:
    """Read one threshold from the registry the route already owns."""
    return int(registry[metric].minimum_n)


def _comparison_n(registry: dict[str, Any], metric: str) -> int:
    """Return the outcomes a single period needs before it may feed a comparison.

    The table's own floor and the metric's registry threshold, whichever is
    stricter: a three-outcome period cannot support a statement about worsening,
    and a ratio the registry withholds cannot support one either. This is the
    approved contract, not this module's invention: the flag rule requires the
    current period and all eight baseline points to be comparable and meet minimum n
    (``shared/self-improvement/DRAFT-MEASUREMENT-ENGINEERING.md``, "Flag rule and
    Needs a look"), and a weekly point below its minimum n is thin data that never
    enters a caption (``shared/self-improvement/DRAFT-MEASUREMENT-DASHBOARD.md``,
    "Thin data", default 20 outcomes).

    Known limit, deliberately not widened here: because a comparison needs nine
    such periods, an agent below 20 outcomes in any one of them produces no automatic
    finding at all, and a quieter agent in the scope also holds back the scope's
    clean verdict (see ``derive_findings_state``). Changing that floor or that
    aggregation is a product decision, not a defect fix.
    """
    return max(RANKING_MINIMUM_N, _minimum_n(registry, metric))


def _denominator(row: dict[str, Any]) -> int:
    return int(row.get("outcomes") or 0)


def _measured_cost(row: dict[str, Any]) -> bool:
    """Say whether one agent-period carries usage at all.

    An agent-period with no usage rows is unmeasured, not free: reading its cost as
    zero would rank an untracked agent as the cheapest one on the surface.
    """
    return int(row.get("usage_rows") or 0) > 0


def _metric_value(registry: dict[str, Any], metric: str, row: dict[str, Any],
                  measured: Callable[[str, str], bool] = lambda _metric, _period: True) -> float | None:
    """Compute one served metric for one aggregate row, or report it unmeasured."""
    if not measured(metric, row.get("period") or ""):
        return None
    outcomes = _denominator(row)
    if not outcomes or outcomes < _minimum_n(registry, metric):
        return None
    if metric == "first_try_acceptance":
        return float(row.get("first_try") or 0) / outcomes
    if metric == "asks_per_outcome":
        return (float(row.get("raised") or 0) + float(row.get("user_holds") or 0)) / outcomes
    if metric == "fresh_tokens_per_outcome":
        if not _measured_cost(row) or row.get("tokens") is None:
            return None
        return float(row["tokens"]) / outcomes
    if metric == "active_hours_per_outcome":
        # No human message to that agent that period: unknown attention, not zero.
        if row.get("active_hours") is None:
            return None
        return float(row["active_hours"]) / outcomes
    return float(outcomes)


def _blank_entry(agent: dict[str, Any], period: str) -> dict[str, Any]:
    return {**agent, "period": period, "outcomes": 0, "first_try": 0, "raised": 0, "user_holds": 0, "tokens": None,
            "usage_rows": 0, "active_hours": None}


def _agent_rows(db, params: dict[str, Any], registry: dict[str, Any]) -> list[dict[str, Any]]:
    """Read one aggregate row per agent and completed period from the prepared grains.

    ``params`` carries the page ``grain``; the rows' ``period`` key holds each period's first local date."""
    agents = {
        row["agent_id"]: dict(row)
        for row in db.execute(
            f"""
            SELECT t.id AS agent_id,t.title,t.project_id,p.name AS project_name
            FROM threads t LEFT JOIN projects p ON p.id=t.project_id
            WHERE t.id IN (SELECT id FROM {PREPARED_ROOTS})
              AND t.user_id=:user_id AND COALESCE(t.agent_enabled,0)=1
              AND COALESCE(t.agent_status,'active')='active'
            """,
            query_params(params),
        ).fetchall()
    }
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for family in sorted({registry[metric].family for metric in TABLE_METRICS}):
        for item in (query_root_active(db, params) if family == "active" else query_family(db, family, params, grouping="root")):
            root_id = item.pop("root_id")
            if root_id not in agents:
                continue
            entry = rows.setdefault((root_id, item["period"]), _blank_entry(agents[root_id], item["period"]))
            entry.update(item)
    for row in db.execute(
        f"""
        SELECT root_id AS agent_id,period,json_group_object(difficulty,count) AS difficulty_mix
        FROM {PREPARED_ROOT_DIFFICULTIES} GROUP BY root_id,period
        """
    ).fetchall():
        difficulty_entry = rows.get((row["agent_id"], row["period"]))
        if difficulty_entry is not None:
            difficulty_entry["difficulty_mix"] = row["difficulty_mix"]
    for entry in rows.values():
        entry.setdefault("difficulty_mix", "{}")
    # Membership is read once into a set, then answered in O(1) per agent. Scanning
    # the (agent, period) keys per agent cost O(agents x agent-periods) for a yes/no the
    # map already held. The set is built before any thin agent is added and agent ids
    # are unique, so a row inserted for one agent never changes another's answer:
    # same population, same empty-period values, same output order.
    measured_agents = {key[0] for key in rows}
    for agent_id, agent in agents.items():
        if agent_id not in measured_agents:
            # An agent with no measured period still belongs in the population: the
            # table says too few, and the automatic findings say why they compared
            # nothing, instead of the agent quietly not existing.
            rows[(agent_id, "")] = {**_blank_entry(agent, ""), "difficulty_mix": "{}"}
    return sorted(rows.values(), key=lambda item: (item["title"], item["agent_id"], item["period"]))


def _pooled_value(registry: dict[str, Any], metric: str, trailing: list[dict[str, Any] | None],
                  measured: Callable[[str, str], bool] = lambda _metric, _period: True,
                  periods: int = 0) -> float | None:
    """One summary value for a table cell: pooled numerators over pooled outcomes.

    Cost pools only when every period that did work also recorded usage. A gap in
    the cost history is reported as unmeasured rather than averaged away, because
    the pooled denominator would then include outcomes whose spend is unknown. A
    period the metric's own instrument predates is not pooled in either direction --
    it is not data, and it must not dilute the average the cell reports: ``periods``
    is how many of the trailing periods that instrument could observe at all.
    """
    present = [row for row in trailing if row and measured(metric, row["period"])]
    outcomes = sum(_denominator(row) for row in present)
    if metric == "outcomes":
        return float(outcomes) / periods if periods else None
    if not outcomes or outcomes < _minimum_n(registry, metric):
        return None
    if metric == "first_try_acceptance":
        return float(sum(int(row.get("first_try") or 0) for row in present)) / outcomes
    if metric == "asks_per_outcome":
        raised = sum(int(row.get("raised") or 0) + int(row.get("user_holds") or 0) for row in present)
        return float(raised) / outcomes
    if metric == "active_hours_per_outcome":
        # Pool only periods that recorded attention, never a zero for one that did not.
        timed = [row for row in present if row.get("active_hours") is not None]
        timed_outcomes = sum(_denominator(row) for row in timed)
        if timed_outcomes < _minimum_n(registry, metric):
            return None
        return sum(float(row["active_hours"]) for row in timed) / timed_outcomes
    if any(_denominator(row) and not _measured_cost(row) for row in present):
        return None
    tokens = sum(float(row.get("tokens") or 0) for row in present if _measured_cost(row))
    return tokens / outcomes


def _worsening(registry: dict[str, Any], rows: dict[str, dict[str, Any]], metric: str,
               frame: list[str], comparable: Callable[[str, str], bool],
               measured: Callable[[str, str], bool] = lambda _metric, _period: True) -> dict[str, Any]:
    """Compare the last completed period with the eight periods before it.

    The frame is the grain's calendar periods, not the nine that happen to have data: a
    gap inside the baseline is not bridged by an older period, and a thin or
    unmeasured period makes the metric uncomparable instead of quietly shortening
    the baseline. The caller's ``comparable`` is the same bound the series marks
    rows with, so a per-outcome metric is never compared across the period where
    its own unit changed: that would report a change in counting as a change in
    the work. ``sd`` is the eight-point sample standard deviation, and the
    threshold is strict, so exactly two deviations is not a finding.
    """
    minimum_n = _comparison_n(registry, metric)
    # A frame period the metric's instrument did not observe carries no number, so the
    # baseline is not merely thin, it is incomplete: the existing reason says that in
    # the vocabulary the surface already serves.
    if any(period not in rows or not measured(metric, period) or _denominator(rows[period]) < minimum_n
           or not comparable(metric, period) for period in frame):
        return {"eligible": False, "reason": "too_little_history", "metric": metric, "minimum_n": minimum_n}
    measured_values = [_metric_value(registry, metric, rows[period], measured) for period in frame]
    if any(value is None for value in measured_values):
        return {"eligible": False, "reason": "not_measured", "metric": metric, "minimum_n": minimum_n}
    values = [value for value in measured_values if value is not None]
    baseline, current_value = values[:-1], values[-1]
    mean = sum(baseline) / BASELINE_PERIODS
    sd = sqrt(sum((value - mean) ** 2 for value in baseline) / (BASELINE_PERIODS - 1))
    adverse = mean - current_value if metric in HIGHER_IS_BETTER else current_value - mean
    zero_variance = sd == 0
    return {
        "eligible": True,
        "metric": metric,
        "minimum_n": minimum_n,
        "window": {"from": frame[0], "to": frame[-1]},
        "mean": mean,
        "sd": sd,
        "current": current_value,
        "adverse_size": adverse / sd if sd else None,
        "zero_variance_baseline": zero_variance,
        "relative_adverse_change": adverse / abs(mean) if zero_variance and mean else None,
        "flagged": adverse > 2 * sd if sd else adverse > 0,
    }



def _active_minutes(window: list[dict[str, Any] | None], measured: Callable[[str, str], bool]) -> float | None:
    """The window's recorded human attention in minutes, pooled the way minutes per outcome pools it."""
    timed = [row for row in window if row and row.get("active_hours") is not None
             and measured("active_hours_per_outcome", row["period"])]
    return 60 * sum(float(row["active_hours"]) for row in timed) if timed else None


def _aggregate_agent(registry: dict[str, Any], agent: dict[str, Any], grain: Grain, metric: str,
                     comparable: Callable[[str, str], bool],
                     measured: Callable[[str, str], bool]) -> dict[str, Any]:
    """Build one public table row for the grain's latest window; no task-level fact leaves here.

    Each metric also carries the grain's previous window, pooled the same way, so a reader
    compares the two windows without recomputing either."""
    rows = agent["periods"]
    trailing_periods = list(grain.latest.periods)
    trailing = [rows.get(period) for period in trailing_periods]
    # Only periods this scope's outcome instrument observed count toward the row's n:
    # the same clip the panels and the per-period points apply, so the table and the
    # series cannot answer for different populations of the same agent.
    outcomes = sum(_denominator(item) for item in trailing
                   if item and measured("outcomes", item["period"]))
    mix: dict[str, int] = defaultdict(int)
    metrics: dict[str, dict[str, Any]] = {}
    for name in TABLE_METRICS:
        points = [
            {
                "period": period,
                "value": _metric_value(registry, name, item, measured) if item else None,
                "n": _denominator(item) if item and measured(name, period) else 0,
                "minimum_n": _minimum_n(registry, name),
            }
            for period, item in zip(trailing_periods, trailing, strict=True)
        ]
        metrics[name] = {
            "value": _pooled_value(registry, name, trailing, measured,
                                   sum(1 for period in trailing_periods if measured(name, period))),
            "n": sum(_denominator(item) for item in trailing
                     if item and measured(name, item["period"])),
            "minimum_n": _minimum_n(registry, name),
            "periods": points,
        }
    for period_row in trailing:
        # The mix explains the count, so it pools exactly the periods the count does:
        # a period the outcome instrument predates is in neither.
        if period_row and measured("outcomes", period_row["period"]):
            for name, count in json.loads(period_row["difficulty_mix"]).items():
                mix[name] += int(count)
    previous_periods = grain.previous.periods
    previous = [rows.get(period) for period in previous_periods]
    for name in TABLE_METRICS:
        measured_periods = sum(1 for period in previous_periods if measured(name, period))
        metrics[name]["previous_value"] = _pooled_value(registry, name, previous, measured, measured_periods)
        if name == "active_hours_per_outcome" and measured_periods < len(previous_periods):
            # Minutes were recorded for only part of the previous window, so its pooled
            # figure covers fewer weeks than the latest: a change against it is a change in
            # coverage. No previous figure; the table reads "new" and Biggest changes skips it.
            metrics[name]["previous_value"] = None
        metrics[name]["previous_n"] = sum(_denominator(item) for item in previous
                                          if item and measured(name, item["period"]))
    frame = list(grain.periods[-FRAME_PERIODS:])
    return {
        "agent": {"id": agent["id"], "name": agent["name"], "project": agent["project"]},
        # The row's n is always its completed-outcome count: the registry defines
        # every served id's denominator that way, so it cannot depend on which
        # column the reader selected.
        "n": outcomes,
        # The human minutes spent on this agent in the latest window (None when no period recorded any).
        "active_minutes": _active_minutes(trailing, measured),
        "difficulty_mix": dict(sorted(mix.items())),
        "status": "eligible" if outcomes >= RANKING_MINIMUM_N else "too_few",
        "rank": None,
        "metrics": metrics,
        "selected_metric": metric,
        "worsening": _worsening(registry, rows, metric, frame, comparable, measured),
        "flags": {name: _worsening(registry, rows, name, frame, comparable, measured)
                  for name in FINDING_METRICS},
    }


def _rank(registry: dict[str, Any], rows: list[dict[str, Any]], metric: str, sort: str) -> list[dict[str, Any]]:
    """Rank the eligible rows on the selected metric and order the table."""
    higher_is_better = metric in HIGHER_IS_BETTER
    ranked = [row for row in rows if row["status"] == "eligible" and row["metrics"][metric]["value"] is not None]
    ranked.sort(key=lambda row: ((-1 if higher_is_better else 1) * row["metrics"][metric]["value"],
                                 row["agent"]["name"], row["agent"]["id"]))
    for index, row in enumerate(ranked, 1):
        row["rank"] = index
    if sort == "ranked":
        return sorted(rows, key=lambda row: (row["rank"] is None, -(row["rank"] or 0),
                                             row["agent"]["name"], row["agent"]["id"]))
    return sorted(rows, key=lambda row: (not row["worsening"]["eligible"],
                                         _severity_key(row["worsening"].get("adverse_size"),
                                                       row["worsening"].get("relative_adverse_change")),
                                         row["agent"]["name"], row["agent"]["id"]))


def build_agents(db, params: dict[str, Any], registry: dict[str, Any], metric: str, sort: str,
                 grain: Grain, comparable: Callable[[str, str], bool],
                 measured: Callable[[str, str], bool], with_dollars: bool = True,
                 ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[dict[str, Any]]], int]:
    """Return ranked rows, findings, PL-4 project summaries and the agent population.

    ``registry``, ``comparable`` and ``measured`` are the caller's: the route that owns
    the ids, thresholds, comparability bound and instrument starts passes its own, so
    this module cannot drift into a second measurement vocabulary or a second date.
    ``grain`` is the caller's request calendar too -- one read per request owns both the
    prepared window and the periods these rows answer for, so no period can fall outside
    the data prepared for it. The table pools the grain's latest window and carries the
    previous window beside it; the findings frame is its last nine periods; the project
    summaries count outcomes in the latest window.
    """
    # Invariants, not caller validation: the route answers an unknown metric or sort
    # with a 400 before any of this runs, so reaching here with one is a server fault.
    if metric not in TABLE_METRICS:
        raise ValueError("unknown or unavailable agents metric")
    if sort not in AGENTS_SORTS:
        raise ValueError("unknown sort")
    missing = [name for name in (*TABLE_METRICS, *FINDING_METRICS)
               if name not in registry or not registry[name].available]
    if missing:
        raise ValueError(f"registry no longer serves {' '.join(missing)}")
    query = {**params, "grain": grain}
    agents: dict[str, dict[str, Any]] = {}
    for item in _agent_rows(db, query, registry):
        agent = agents.setdefault(item["agent_id"], {"id": item["agent_id"], "name": item["title"],
                                                     "project": {"id": item["project_id"], "name": item["project_name"]},
                                                     "periods": {}})
        if item["period"]:
            agent["periods"][item["period"]] = item
    aggregated = [_aggregate_agent(registry, agent, grain, metric, comparable, measured) for agent in agents.values()]
    # Dollars per hour reads each task's whole chain, so it covers only the two windows the table shows.
    # ``with_dollars=False`` is for a reader of the other columns only (Biggest changes), so it does not pay for it.
    costs = query_task_hour_cost(db, {**query, "grain": two_windows(grain)}) if with_dollars else None
    for row in aggregated if costs is not None else ():
        row["metrics"][DOLLARS_PER_HOUR] = dollars_metric(costs, row["agent"]["id"], grain, RANKING_MINIMUM_N)
    rows = _rank(registry, aggregated, metric, sort)
    findings = _findings(rows)
    return rows, findings, _project_summaries(db, rows, grain), len(agents)