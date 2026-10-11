"""Measurement's "Biggest changes": what moved most in the grain's latest window.

A row compares one subject's latest window (7 days or 4 weeks) with the window before it:
the scope itself on the pooled windows ``/series`` serves (and, for $ per hour of work,
the ``/cost`` headline's own windows, read as served), and each agent on the pooled
windows the Agents table already serves, and each model on its biggest size class in the
Models table. Nothing here selects rows, sums or divides across outcomes (a unit change
aside): it reads what ``/series``, ``/agents`` and ``/models`` return, and ranks the change.
An agent row's "overall" is the scope's own figure for the measure (all its agents' tasks
pooled for $ per hour of work); a model row's is every model's tasks of that size class
pooled; a scope row has none.

The rules are fixed so the page reads the same on every install:

* both windows need at least ``MINIMUM_N`` outcomes (a model's size class: the Models
  table's own 14-task floor), and the measure must be comparable across them (no
  definition break between the two windows);
* a move counts from ``MINIMUM_CHANGE`` (10 percent) in either direction;
* the worse list leads with the largest move and holds one row per agent or model, its
  largest worse move; the improved list is the scope's own page measures that moved the
  other way, largest first;
* each measure carries one fixed action line, identical for every subject.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from api.measurement_agent_cost import DOLLARS_PER_HOUR
from api.measurement_cost import cost_read
from api.measurement_scope import resolve_grain, validate_scope
from core.measurement_scope import Grain
from infra.auth import require_user
from infra.database import get_db
from infra.db_async import run_in_db

router = APIRouter(prefix="/api/measurement", tags=["measurement"])

MINIMUM_N = 20
MINIMUM_CHANGE = 0.10
MAX_ROWS = 6


class Measure:
    """One comparable measure: its series, direction, display and fixed action line."""

    def __init__(self, series: str, label: str, higher_is_better: bool, fmt: str, action: str,
                 *, scale: float = 1.0, per_agent: bool = True, from_series: bool = True, per_model: bool = False,
                 from_cost: bool = False, scope_label: str | None = None) -> None:
        self.series, self.label, self.higher_is_better, self.format = series, label, higher_is_better, fmt
        self.action, self.scale, self.per_agent = action, scale, per_agent
        # ``from_series``: the scope's own windows come from ``/series``; otherwise the measure has no series id.
        # ``from_cost``: the scope's own windows are the Cost headline's ``latest`` and ``previous``.
        self.from_series, self.per_model, self.from_cost = from_series, per_model, from_cost
        # ``scope_label``: the scope row's wording where it differs from an agent row's ("Your minutes per outcome").
        self.scope_label = scope_label


#: measure id -> definition. The action lines are the page's fixed wording.
MEASURES: dict[str, Measure] = {
    "hours_per_hour": Measure(
        "estimated_human_hours_per_active_hour", "Work per hour you spend", True, "hours",
        "Check which outcomes took the most of your time.", per_agent=False),
    "minutes_per_outcome": Measure(
        "active_hours_per_outcome", "Minutes per outcome", False, "minutes",
        "Review its recent send-backs and how it asks you.", scale=60.0,
        scope_label="Your minutes per outcome"),
    "asks_per_outcome": Measure(
        "asks_per_outcome", "Asks per outcome", False, "ratio",
        "Review what keeps being asked, and whether guidance answers it.", per_agent=False),
    "first_try_acceptance": Measure(
        "first_try_acceptance", "First try", True, "percent",
        "Read the latest rejected outcomes for the shared cause."),
    # The Cost headline's own figure, read as served; no agent or model is ranked on it.
    "cost_per_hour": Measure(
        "cost_per_hour", "Cost per hour of delivered work", False, "dollars",
        "Check which models and size classes drive the cost.", per_agent=False, from_series=False, from_cost=True),
    # Agents only: the scope's figure is every agent's tasks pooled, served beside the rows.
    "dollars_per_hour": Measure(
        DOLLARS_PER_HOUR, "$ per hour of work", False, "dollars",
        "Check which size class and model drive the cost.", from_series=False),
    # Models only: the label names the size class, and each row reads its model's biggest comparable class.
    "cost_per_task": Measure(
        "cost_per_task", "$ per task", False, "dollars",
        "Check whether this model is the right fit for this size class.",
        per_agent=False, from_series=False, per_model=True),
}
ACTIONS_BY_MEASURE = {key: measure.action for key, measure in MEASURES.items()}
SERIES_IDS = tuple(dict.fromkeys(measure.series for measure in MEASURES.values() if measure.from_series))


def _item(subject: dict[str, str], key: str, before: float, after: float, n: int, average: float | None,
          label: str | None = None) -> dict[str, Any] | None:
    """One ranked row, or None when the move is under the minimum change or starts at zero."""
    if not before:
        return None
    change = (after - before) / before
    if abs(change) < MINIMUM_CHANGE:
        return None
    measure = MEASURES[key]
    return {
        "subject": subject, "measure": key, "label": label or measure.label, "format": measure.format,
        "previous": before, "latest": after, "change": change, "n": n, "overall": average,
        "action": measure.action, "worse": (change < 0) == measure.higher_is_better,
    }


def _scope_items(
    scope: dict[str, str], windows: dict[str, dict[str, Any]], comparable: Any, firsts: tuple[str, str],
    cost: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The scope's own page measures, read from the ``/series`` windows and the Cost headline."""
    items: list[dict[str, Any]] = []
    for key, measure in MEASURES.items():
        pair: dict[str, Any]
        if measure.from_cost:
            # The Cost headline's $ per hour and the outcomes sized behind it, as ``/cost`` serves them.
            served: dict[str, Any] = cost or {}
            pair = {window: {"value": (served.get(window) or {}).get("per_hour"), "n": (served.get(window) or {}).get("sized") or 0}
                    for window in ("previous", "latest")}
        elif measure.from_series:
            pair = windows.get(measure.series) or {}
        else:
            continue
        before, after = pair.get("previous"), pair.get("latest")
        if not before or not after or before["value"] is None or after["value"] is None:
            continue
        if before["n"] < MINIMUM_N or after["n"] < MINIMUM_N or not all(comparable(measure.series, first) for first in firsts):
            continue
        item = _item(scope, key, before["value"] * measure.scale, after["value"] * measure.scale, after["n"], None, measure.scope_label)
        if item:
            items.append(item)
    return items


def _agent_items(
    agents: dict[str, Any] | None, windows: dict[str, dict[str, Any]], comparable: Any, firsts: tuple[str, str],
) -> list[dict[str, Any]]:
    """Each agent's moves, read against the scope's figure for the measure (the agents' own pooled one where it has no series)."""
    averages = {series: (pair.get("latest") or {}).get("value") for series, pair in windows.items()}
    averages.update({series: cell.get("value") for series, cell in ((agents or {}).get("overall") or {}).items()})
    items: list[dict[str, Any]] = []
    for row in (agents or {}).get("rows", []):
        subject = {"id": row["agent"]["id"], "name": row["agent"]["name"], "kind": "agent"}
        for key, measure in MEASURES.items():
            metric = (row.get("metrics", {}).get(measure.series) or {}) if measure.per_agent else {}
            before, after = metric.get("previous_value"), metric.get("value")
            if before is None or after is None:
                continue
            if (metric.get("previous_n") or 0) < MINIMUM_N or (metric.get("n") or 0) < MINIMUM_N:
                continue
            if not all(comparable(measure.series, first) for first in firsts):
                continue
            average = averages.get(measure.series)
            item = _item(subject, key, before * measure.scale, after * measure.scale, metric["n"],
                         average * measure.scale if average is not None else None)
            if item:
                items.append(item)
    return items


def _model_items(models: dict[str, Any] | None, comparable: Any, firsts: tuple[str, str]) -> list[dict[str, Any]]:
    """Each named model's biggest size class priced in both windows, read against that class pooled across models."""
    items: list[dict[str, Any]] = []
    overall_by_size = {cell["size"]: cell["cost_per_task"] for cell in (models or {}).get("size_overall", [])}
    for row in (models or {}).get("rows", []):
        # The floor row pools unnamed models, so it is not a subject to look at.
        if row.get("other"):
            continue
        cells = [cell for cell in row.get("sizes", []) if cell.get("previous_cost_per_task")]
        if not cells:
            continue
        cell = max(cells, key=lambda cell: cell["tasks"])
        if not all(comparable(MEASURES["cost_per_task"].series, first) for first in firsts):
            continue
        item = _item({"id": row["model"], "name": row["label"], "kind": "model"}, "cost_per_task",
                     cell["previous_cost_per_task"], cell["cost_per_task"], cell["tasks"],
                     overall_by_size.get(cell["size"]), label=f"{cell['size']} $ per task")
        if item:
            items.append(item)
    return items


def build_where_to_look(
    scope: dict[str, str], windows: dict[str, dict[str, Any]], agents: dict[str, Any] | None,
    comparable: Any, grain: Grain, models: dict[str, Any] | None = None, cost: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rank the scope's, its agents' and its models' moves into the worse and the improved lists.

    ``windows`` is the ``/series`` windows block for the scope itself (series id ->
    ``{latest, previous}`` pooled ``{value, n}``), ``agents`` the ``/agents`` payload (None
    in an agent view, where there is no list of agents), ``comparable(series, period)``
    the page's definition-break check, and ``grain`` the one window rule: its latest
    window against its previous one. The scope's latest figure per series is the "overall"
    an agent row is read against, except a measure with no series (``/agents`` ``overall``).
    ``models`` is the ``/models`` payload: each model row is ranked on its biggest size class
    that the Models table prices in both windows, read against that class pooled across models.
    ``cost`` is the ``/cost`` payload, whose headline $ per hour of work is a scope row.

    The worse list holds each agent's and each model's largest worse move once, beside the
    scope's own worse page measures; the improved list holds only the scope's own page measures.
    """
    firsts = (grain.previous.first, grain.latest.first)
    scope_items = _scope_items(scope, windows, comparable, firsts, cost)
    subject_items = _agent_items(agents, windows, comparable, firsts) + _model_items(models, comparable, firsts)
    order = lambda item: (-abs(item["change"]), item["subject"]["name"], item["measure"])
    seen: set[tuple[str, str]] = set()
    worse = [item for item in scope_items if item["worse"]]
    for item in sorted((item for item in subject_items if item["worse"]), key=order):
        who = (item["subject"]["kind"], item["subject"]["id"])
        if who not in seen:
            seen.add(who)
            worse.append(item)
    better = sorted((item for item in scope_items if not item["worse"]), key=order)
    return {
        "worse": sorted(worse, key=order)[:MAX_ROWS], "improved": better[:MAX_ROWS],
        "minimum_n": MINIMUM_N, "minimum_change": MINIMUM_CHANGE, "period": grain.describe(),
    }


@router.get("/where-to-look")
async def measurement_where_to_look(
    project: str = Query(...), agent: str = Query(...), grain: str = "days",
    timezone_name: str = Query(..., alias="timezone"), ids: str | None = None, user: dict = Depends(require_user),
) -> dict[str, Any]:
    """The scope's largest moves, latest window against the one before, worse and improved.

    Every figure comes from the handlers the page already calls, run for the
    caller's own scope and grain. An agent view compares that agent alone: it has no
    list of agents to rank.

    ``ids`` is the page's own ``/series`` id list, so this read joins the page's request (or
    takes its result from the last few seconds) instead of running the series again; the
    measures ranked here are added when missing. The agents read is the page's default
    Agents table request, for the same reason.
    """
    from api import measurement as page

    # One request calendar: the series and the agents read this grain, never one resolved again
    # later, so a request that crosses the viewer's midnight cannot describe two splits.
    page_grain = resolve_grain(grain, timezone_name, averaged=True)

    def scope_of() -> dict[str, str]:
        db = get_db()
        try:
            selected = validate_scope(db, user["id"], project, agent)
            if selected.agent:
                row = db.execute("SELECT title FROM threads WHERE id=? AND user_id=?", (selected.agent, user["id"])).fetchone()
                return {"id": selected.agent, "name": row["title"] if row else "Agent", "kind": "agent"}
            if selected.project == "all":
                return {"id": "all", "name": "All work", "kind": "scope"}
            row = db.execute("SELECT name FROM projects WHERE id=?", (selected.project,)).fetchone()
            return {"id": selected.project, "name": row["name"] if row else "Project", "kind": "scope"}
        finally:
            db.close()

    scope = await run_in_db(scope_of)
    requested = page.requested_series(",".join(dict.fromkeys([*(ids or "").split(","), *SERIES_IDS])))
    series = await page.series_payload(requested, project, agent, page_grain, user, reuse_finished=True)
    # The page's Models table request, so a page load that drew it shares the read.
    models = await page.models_read(project, agent, page_grain.unaveraged(), user, reuse_finished=True)
    # The page's Cost request: its headline $ per hour of work is read as served, never computed again here.
    cost = await cost_read(project, agent, page_grain, user, reuse_finished=True)
    agents = None
    if scope["kind"] != "agent":
        # The page's default Agents table: its dollars column is not a measure ranked here.
        agents = await page.agents_payload(project, agent, page_grain.unaveraged(), "ranked", "fresh_tokens_per_outcome",
                                           True, user, reuse_finished=True)
    return build_where_to_look(scope, series["windows"], agents, page.period_comparable, page_grain, models, cost)
