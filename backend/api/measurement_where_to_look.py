"""Measurement's "Biggest changes": what moved most in the grain's latest window.

A row compares one subject's latest window (7 days or 4 weeks) with the window before it:
the scope itself on the pooled windows ``/series`` serves, and each agent on the pooled
windows the Agents table already serves. Nothing here selects rows, sums or divides across
outcomes (a unit change aside): it reads what ``/series`` and ``/agents`` return, and ranks
the change. An agent row's "overall" is the scope's own figure for the measure; a scope row has none.

The rules are fixed so the page reads the same on every install:

* both windows need at least ``MINIMUM_N`` outcomes, and the measure must be comparable
  across them (no definition break between the two windows);
* a move counts from ``MINIMUM_CHANGE`` (10 percent) in either direction;
* the worse list leads with the largest move; the improved list is the same rows the
  other way, largest first;
* each measure carries one fixed action line, identical for every subject.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

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
                 *, scale: float = 1.0, per_agent: bool = True) -> None:
        self.series, self.label, self.higher_is_better, self.format = series, label, higher_is_better, fmt
        self.action, self.scale, self.per_agent = action, scale, per_agent


#: measure id -> definition. The action lines are the page's fixed wording.
MEASURES: dict[str, Measure] = {
    "hours_per_hour": Measure(
        "estimated_human_hours_per_active_hour", "Work per hour of your attention", True, "hours",
        "Check which outcomes took the most of your time.", per_agent=False),
    "minutes_per_outcome": Measure(
        "active_hours_per_outcome", "Minutes per outcome", False, "minutes",
        "Review recent send-backs and how work is asked of you.", scale=60.0),
    "asks_per_outcome": Measure(
        "asks_per_outcome", "Asks per outcome", False, "ratio",
        "Review what keeps being asked, and whether guidance answers it."),
    "first_try_acceptance": Measure(
        "first_try_acceptance", "First try", True, "percent",
        "Read the latest rejected outcomes for the shared cause."),
    "fresh_tokens_per_outcome": Measure(
        "fresh_tokens_per_outcome", "Fresh tokens per outcome", False, "tokens",
        "Check which tasks use the most tokens, and whether a lighter model fits."),
}
ACTIONS_BY_MEASURE = {key: measure.action for key, measure in MEASURES.items()}
SERIES_IDS = tuple(dict.fromkeys(measure.series for measure in MEASURES.values()))


def _item(subject: dict[str, str], key: str, before: float, after: float, n: int, average: float | None,
          ) -> dict[str, Any] | None:
    """One ranked row, or None when the move is under the minimum change or starts at zero."""
    if not before:
        return None
    change = (after - before) / before
    if abs(change) < MINIMUM_CHANGE:
        return None
    measure = MEASURES[key]
    return {
        "subject": subject, "measure": key, "label": measure.label, "format": measure.format,
        "previous": before, "latest": after, "change": change, "n": n, "overall": average,
        "action": measure.action, "worse": (change < 0) == measure.higher_is_better,
    }


def build_where_to_look(
    scope: dict[str, str], windows: dict[str, dict[str, Any]], agents: dict[str, Any] | None,
    comparable: Any, grain: Grain,
) -> dict[str, Any]:
    """Rank the scope's and its agents' moves into the worse and the improved lists.

    ``windows`` is the ``/series`` windows block for the scope itself (series id ->
    ``{latest, previous}`` pooled ``{value, n}``), ``agents`` the ``/agents`` payload (None
    in an agent view, where there is no list of agents), ``comparable(series, period)``
    the page's definition-break check, and ``grain`` the one window rule: its latest
    window against its previous one. The scope's latest figure per series is the "overall"
    an agent row is read against.
    """
    items: list[dict[str, Any]] = []
    firsts = (grain.previous.first, grain.latest.first)
    averages = {series: (pair.get("latest") or {}).get("value") for series, pair in windows.items()}
    for key, measure in MEASURES.items():
        pair = windows.get(measure.series) or {}
        before, after = pair.get("previous"), pair.get("latest")
        if not before or not after or before["value"] is None or after["value"] is None:
            continue
        if before["n"] < MINIMUM_N or after["n"] < MINIMUM_N or not all(comparable(measure.series, first) for first in firsts):
            continue
        item = _item(scope, key, before["value"] * measure.scale, after["value"] * measure.scale, after["n"], None)
        if item:
            items.append(item)
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
    order = lambda item: (-abs(item["change"]), item["subject"]["name"], item["measure"])
    worse = sorted((item for item in items if item["worse"]), key=order)
    better = sorted((item for item in items if not item["worse"]), key=order)
    return {
        "worse": worse[:MAX_ROWS], "improved": better[:MAX_ROWS],
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
    agents = None
    if scope["kind"] != "agent":
        # The page's default Agents table: its dollars column is not a measure ranked here.
        agents = await page.agents_payload(project, agent, page_grain.unaveraged(), "ranked", "fresh_tokens_per_outcome",
                                           True, user, reuse_finished=True)
    return build_where_to_look(scope, series["windows"], agents, page.period_comparable, page_grain)
