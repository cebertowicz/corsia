"""Read-only Measurement API handlers."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from threading import Event
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from api.measurement_agents import AGENTS_SORTS, TABLE_METRICS, build_agents
from api.measurement_average import add_averages
from api.measurement_cost import register_cost_route
from api.measurement_findings import derive_findings_state
from api.measurement_join import joined_read
from api.measurement_limits import first_recorded_period, limits_by_period, router as limits_router
from api.measurement_recent import router as recent_router
from api.measurement_presence import PresenceCoverage, presence_coverage
from api.measurement_corrections import corrections_family
from api.measurement_breaks import (
    COMPARABILITY_UNRESTRICTED, comparability_start, derive_breaks, first_availability_period,
    next_comparability_boundary, period_comparable,
)
from api.measurement_registry import REGISTRY, period_measured
from api.measurement_series_selection import metric_spine, requested_series
from api.measurement_models import models_read, task_chain_costs
from api.measurement_partial import current_period_outcomes
from api.measurement_queries import prepare_outcome_periods, query_family
from api.measurement_size import size_family, size_rows
from api.measurement_windows import series_windows
from api.measurement_scope import resolve_grain, scope_cte, validate_scope
from infra.auth import require_user
from infra.database import get_db
from infra.db_async import run_bulk
from infra.config import WORKSPACE
from core.lesson_report_access import report_for_identity
from core.lesson_report_offers import LessonReportCollectionCancelled
from core.measurement_scope import Grain, ScopeError

router = APIRouter(prefix="/api/measurement", tags=["measurement"])
router.include_router(limits_router)
router.include_router(recent_router)


async def _monitor_client_disconnect(request: Request, cancellation_event: Event) -> None:
    """Signal collection to stop after the current SQLite chunk when its client leaves.

    The handler also sets the event when it is done, which ends this loop on its own:
    ``is_disconnected`` polls inside a cancelled anyio scope that can absorb the task
    cancellation, and a watcher that outlives its handler holds the response back.
    """
    while not cancellation_event.is_set() and not await request.is_disconnected():
        await asyncio.sleep(0.05)
    cancellation_event.set()


@router.get("/lessons")
async def measurement_lessons(
    request: Request, project: str = Query(...), agent: str = Query(...), range: str = "12w",
    lesson_id: str | None = None, version: str | None = None,
    cursor: str | None = Query(None, max_length=1024), preview: bool = False,
    evidence: str | None = None, user: dict = Depends(require_user),
):
    """Read the same bounded lesson report as the agent tool for this view."""
    cancellation_event = Event()
    monitor = asyncio.create_task(_monitor_client_disconnect(request, cancellation_event))
    view = "agent" if agent != "none" else "all" if project == "all" else "project"
    try:
        return await report_for_identity(
            user_id=user["id"], workspace=WORKSPACE, scope=view,
            project=project, agent=agent, range=range, lesson_id=lesson_id,
            version=version, cursor=cursor, preview=preview, evidence=evidence,
            cancellation_event=cancellation_event,
        )
    except LessonReportCollectionCancelled:
        return Response(status_code=204)
    except ScopeError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except (ValueError, PermissionError) as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        cancellation_event.set()
        monitor.cancel()
        try:
            await monitor
        except asyncio.CancelledError:
            pass


def _row(metric: str, period: str, source: dict[str, Any] | None, scoped_agent: bool, partial: bool = False,
         grain: Grain | None = None, first_presence_period: str | None = None, source_start: str | None = None,
         breaks: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Apply registry-owned thresholds and presentation flags to one aggregate row.

    ``partial`` marks the period still in progress, served for ``fresh_tokens`` and, on Days, for ``outcomes``.
    """
    definition = REGISTRY[metric]
    source = source or {}
    n = int(source.get("outcomes", source.get("answered", source.get("n", 0))) or 0)
    if metric == "outcomes":
        # Once the install-wide outcomes instrument exists, an earlier empty
        # selected scope is an observed zero rather than a missing value.
        value: float | int | None = source.get("outcomes", 0)
    elif metric == "first_try_acceptance": value = source.get("first_try", 0) / n if n else None
    elif metric == "rejections_per_outcome": value = source.get("rejections", 0) / n if n else None
    elif metric == "active_hours_per_outcome": value = source.get("active_hours", 0) / n if n else None
    elif metric == "outcomes_per_active_hour":
        active_hours = source.get("active_hours", 0)
        value = n / active_hours if active_hours else None
    elif metric == "time_on_platform_h": value, n = source.get("value", 0), int(source.get("n", 0) or 0)
    elif metric == "limit_hits": value, n = int(source.get("hits", 0) or 0), int(source.get("hits", 0) or 0)
    elif metric == "asks_per_outcome": value = (source.get("raised", 0) + source.get("user_holds", 0)) / n if n else None
    elif metric == "corrections_per_outcome": value = source.get("corrections", 0) / n if n else None
    elif metric == "asks_plus_corrections_per_outcome":
        value = (source.get("raised", 0) + source.get("user_holds", 0) + source.get("corrections", 0)) / n if n else None
    elif metric == "took_recommendation_pct":
        n = int(source.get("answered", 0) or 0)
        value = source.get("recommendation", 0) / n if n else None
    elif metric == "answered_other_pct":
        n = int(source.get("answered", 0) or 0)
        value = source.get("other", 0) / n if n else None
    elif metric == "answered_neither_pct":
        n = int(source.get("answered", 0) or 0)
        value = source.get("neither", 0) / n if n else None
    elif metric == "fresh_tokens":
        # The calls behind the period: it is measured when any were recorded. A period whose
        # every call carried no count has no figure, and is never served as zero tokens.
        n = int(source.get("usage_rows", 0) or 0)
        unknown_records = int(source.get("unknown_records", 0) or 0)
        value = None if not n or (unknown_records and not source.get("tokens")) else int(source.get("tokens") or 0)
    else: value = source.get("tokens", 0) / n if n else None
    thin = scoped_agent and metric in {"took_recommendation_pct", "answered_other_pct", "answered_neither_pct"} and n < 6
    if not period_measured(metric, period, first_presence_period, source_start):
        # An instrument measures nothing before it exists. The period is served as not
        # measured -- never as a zero the chart could draw a baseline through.
        value, n = None, 0
    raw_value = value
    if n < definition.minimum_n or thin: value = None
    comparable = period_comparable(period, breaks or [])
    result = {"period": period, "series": metric, "method_version": definition.method_version, "value": value, "raw_value": raw_value, "n": n, "proxy": definition.proxy,
              "partial": partial, "comparable": comparable, "break": None,
              "dimension": None, "thin": thin}
    if metric == "fresh_tokens":
        # The value is a lower bound by ``unknown`` calls; ``cached`` is the cache re-reads the value leaves out.
        result["unknown"] = int(source.get("unknown_records", 0) or 0)
        result["cached"] = int(source.get("cached_tokens", 0) or 0)
    return result


@router.get("/series")
async def measurement_series(
    ids: str = Query(...), project: str = Query(...), agent: str = Query(...), grain: str = "days",
    timezone_name: str = Query(..., alias="timezone"), user: dict = Depends(require_user),
):
    """Return selected completed-period aggregates for one composite scope, grain and viewer zone."""
    requested = requested_series(ids)
    return await series_payload(requested, project, agent, resolve_grain(grain, timezone_name, averaged=True), user)



def _series_read(requested: list[str], project: str, agent: str, page_grain: Grain, user_id: str) -> tuple[dict[str, str | None], dict[str, list[dict[str, Any]]], str | None, int | None, PresenceCoverage, dict[str, list[dict[str, str]]]]:
    db = get_db()
    try:
        selected_scope = validate_scope(db, user_id, project, agent)
        scope_params = selected_scope.params(user_id)
        params = {**scope_params, "grain": page_grain}
        so_far = current_period_outcomes(db, scope_params, page_grain) if "outcomes" in requested and page_grain.kind == "days" else None
        families = {REGISTRY[metric].family for metric in requested}
        coverage = presence_coverage(db, user_id, page_grain.zone)
        first_limit_period = first_recorded_period(db, params) if "limits" in families else None
        breaks = derive_breaks(db, requested, page_grain)
        prepare_outcome_periods(db, params)
        decision_rows = query_family(db, "decisions", params) if families & {"decisions", "corrections"} else []
        family_rows = {family: (size_family(db, params) if family == "size" else
                                [{"period": period, "hits": hits} for period, hits in limits_by_period(db, params).items()] if family == "limits" else
                                corrections_family(db, page_grain, decision_rows) if family == "corrections" else
                                decision_rows if family == "decisions" else
                                query_family(db, family, params))
                       for family in families}
        return selected_scope.response(), family_rows, first_limit_period, so_far, coverage, breaks
    finally:
        db.close()


@joined_read
async def series_payload(requested: list[str], project: str, agent: str, page_grain: Grain, user: dict) -> dict[str, Any]:
    """The ``/series`` body for one resolved grain, which must carry the averaging lead-in.

    A caller that also reads another Measurement route passes the grain it resolved once, so
    every part of its response is bucketed by one request calendar. An identical read in flight is
    joined (``api.measurement_join``).
    """
    # The period still in progress: every series is served by completed periods, and only the token
    # total (and, on Days, the outcome count) also shows this one, flagged partial, so a first period is not an empty page.
    current_period = page_grain.current

    selected_scope, family_rows, first_limit_period, outcomes_so_far, coverage, breaks = await run_bulk(
        _series_read, requested, project, agent, page_grain, user["id"],
    )
    first_presence = coverage.start_period(page_grain)
    rows: list[dict[str, Any]] = []
    windows: dict[str, dict[str, Any]] = {}
    spine = list(page_grain.periods)
    def measured(metric: str, period: str) -> bool:
        return period_measured(metric, period, first_presence, first_availability_period(breaks[metric]))

    for metric in requested:
        family = REGISTRY[metric].family
        by_period = {row["period"]: row for row in family_rows[family]}
        sources = by_period if family != "tokens" else {}
        source_start = first_availability_period(breaks[metric])
        eligible_periods = metric_spine(metric, spine, first_limit_period, first_presence, source_start, page_grain)
        eligible, first_row = set(eligible_periods), len(rows)
        drawn = [period for period in eligible_periods if period in page_grain.served]
        # Sized hours run from the instrument's start, but the size rate divides them by presence
        # time, which exists only from the first recorded presence. Before that the rate is not
        # measured: no row or window may divide earlier work by later time.
        rate_start = max((start for start in (source_start, first_presence) if start), default=None)
        rate_period = (rate_start or "") if family == "size" else ""

        def rate_measured(name: str, period: str, start: str = rate_period) -> bool:
            return measured(name, period) and period >= start
        if family == "size":
            rows.extend(size_rows(metric, REGISTRY[metric].minimum_n, REGISTRY[metric].method_version, drawn, family_rows[family],
                                  lambda name, period: next_comparability_boundary(period, breaks[name]), rate_measured,
                                  data_start=rate_period))
        elif family == "tokens":
            by_period = {row["period"]: row for row in family_rows[family]}
            periods = [*drawn, current_period] if metric == "fresh_tokens" else drawn
            if metric == "fresh_tokens":
                started = next((period for period in eligible_periods if (by_period.get(period) or {}).get("usage_rows")), None)
                eligible = {period for period in eligible_periods if started and period >= started}
                # The total starts at its first recorded period; no leading gap or uncounted mark precedes it.
                recorded = [period for period in periods if measured(metric, period) and (by_period.get(period) or {}).get("usage_rows")]
                periods = [period for period in periods if recorded and period >= recorded[0]]
            rows.extend(_row(metric, period, by_period.get(period), selected_scope["agent"] is not None, period == current_period and metric == "fresh_tokens", page_grain, first_presence, source_start, breaks[metric]) for period in periods)
        else:
            rows.extend(_row(metric, period, sources.get(period), selected_scope["agent"] is not None, grain=page_grain, first_presence_period=first_presence, source_start=source_start, breaks=breaks[metric]) for period in drawn)
            if metric == "outcomes" and outcomes_so_far is not None:
                rows.append(_row(metric, current_period, {"outcomes": outcomes_so_far}, False, True, page_grain, first_presence, source_start, breaks[metric]))
        pooling = {"measured": rate_measured, "size_minimum_n": REGISTRY[metric].minimum_n if family == "size" else None,
                   "row": lambda name, period, source: _row(name, period, source, selected_scope["agent"] is not None, grain=page_grain, first_presence_period=first_presence, source_start=first_availability_period(breaks[name]), breaks=breaks[name])}
        # Every series pools the window its card names; a size series' comparability marks its rows, never its window.
        windows[metric] = series_windows(metric, page_grain, by_period, periods=set(eligible_periods), **pooling)
        add_averages(rows[first_row:], metric, page_grain, eligible, by_period, **pooling)
    # The page reads each series' minimum n and the moving average's length from here, never from a copy of its own.
    return {"definition_version": 1, "scope": selected_scope, "period": page_grain.describe(), "average_periods": page_grain.average_periods,
            "rows": rows, "windows": windows, "breaks": breaks, "minimum_n": {metric: REGISTRY[metric].minimum_n for metric in requested}}


@router.get("/series/index")
async def measurement_index(project: str = Query(...), agent: str = Query(...),
                            timezone_name: Annotated[str, Query(alias="timezone")] = "UTC", user: dict = Depends(require_user)):
    """Return the registry and scoped cost metadata for the composite scope.

    Presence-backed and size-rate starts are the viewer-local days ``PresenceCoverage`` gives the series.
    """
    zone = resolve_grain("days", timezone_name).zone

    def query() -> tuple[dict[str, str | None], dict[str, float | int], PresenceCoverage, dict[str, list[dict[str, str]]]]:
        db = get_db()
        try:
            selected_scope = validate_scope(db, user["id"], project, agent)
            params = selected_scope.params(user["id"])
            cost = db.execute(scope_cte() + """
                SELECT COALESCE(SUM(cost_usd),0) AS known_cost_usd, COUNT(*) AS n,
                  COUNT(*)-COUNT(cost_usd) AS unknown_cost_rows
                FROM llm_usage WHERE thread_id IN (SELECT id FROM scope_threads)
            """, params).fetchone()
            total = int(cost["n"] or 0)
            breaks = derive_breaks(db, REGISTRY, resolve_grain("days", zone))
            return selected_scope.response(), {"known_cost_usd": float(cost["known_cost_usd"] or 0),
                                               "unknown_cost_share": (int(cost["unknown_cost_rows"] or 0) / total if total else 0)}, presence_coverage(db, user["id"], zone), breaks
        finally:
            db.close()

    selected_scope, cost_metadata, coverage, breaks = await run_bulk(query)

    def data_start(name: str, item: Any) -> str | None:
        source_start = first_availability_period(breaks[name])
        if item.family == "size":
            return max((start for start in (source_start, coverage.start_day) if start), default=None)
        if item.presence_backed:
            return coverage.start_day or source_start
        return source_start
    definitions = [{"id": name, "display_name": name.replace("_", " ").title(),
                    "definition": f"Weekly aggregate for {name.replace('_', ' ')}.", "unit": item.unit,
                    "minimum_n": item.minimum_n, "owner": "Self-Improvement Infrastructure",
                    "method_version": item.method_version, "data_start": data_start(name, item), "proxy": item.proxy,
                    "proxy_explanation": "Proxy measurement" if item.proxy else None,
                    "comparability": {"before": comparability_start(breaks[name]), "restricted": name not in COMPARABILITY_UNRESTRICTED},
                    "breaks": breaks[name],
                    "availability": "ready" if item.available else "unavailable",
                    "scope_capability": ["project", "agent"], "companion": item.companion}
                   for name, item in REGISTRY.items()]
    return {"definition_version": 1, "scope": selected_scope, "definitions": definitions,
            "cost_metadata": cost_metadata}


@router.get("/agents")
async def measurement_agents(
    project: str = Query(...),
    agent: str = Query(...),
    timezone_name: str = Query(..., alias="timezone"),
    grain: str = "days",
    sort: str = "ranked",
    metric: str = "fresh_tokens_per_outcome",
    dollars: bool = True,
    user: dict = Depends(require_user),
):
    """Return the Agents table, its automatic findings and the PL-4 project summaries.

    The table pools the page grain's latest window (7 days or 4 weeks) and carries the
    window before it for comparison; both come from one ``Grain`` resolved once for the
    request, in the viewer's ``timezone``. A project and the agent whose rows these are are
    required, an unknown metric or sort is a 400, and the ids and thresholds are the
    registry this route already owns.
    """
    # The caller's own parameters are answered here, before any scope lookup or any
    # prepared window, exactly as an unknown series id is in ``/series``. What fails
    # after this point is this server's fault and must not be dressed up as a 400.
    if metric not in TABLE_METRICS:
        raise HTTPException(400, "unknown or unavailable agents metric")
    if sort not in AGENTS_SORTS:
        raise HTTPException(400, "unknown sort")
    return await agents_payload(project, agent, resolve_grain(grain, timezone_name), sort, metric, dollars, user)


@joined_read
async def agents_payload(project: str, agent: str, page_grain: Grain, sort: str, metric: str, dollars: bool,
                         user: dict) -> dict[str, Any]:
    """The ``/agents`` body for one resolved grain and an already validated metric and sort."""
    # The dollars per hour read the task chains Models reads for the same page load: awaited here, outside the
    # lane, so this read holds no worker while it waits for them.
    cost_rows = (await task_chain_costs(project, agent, page_grain, user))["cost_rows"] if dollars else []

    def query() -> dict[str, Any]:
        db = get_db()
        try:
            selected_scope = validate_scope(db, user["id"], project, agent)
            params = {**selected_scope.params(user["id"]), "grain": page_grain}
            first_presence = presence_coverage(db, user["id"], page_grain.zone).start_period(page_grain)
            # One prepared window covers every period the grain draws, so nothing reads live plans.
            breaks = derive_breaks(db, TABLE_METRICS, page_grain)
            prepare_outcome_periods(db, params, roots=True)
            rows, findings, summaries, agent_count, overall = build_agents(
                db, params, REGISTRY, metric, sort, page_grain,
                lambda name, period: period_comparable(period, breaks[name]),
                lambda name, period: period_measured(name, period, first_presence, first_availability_period(breaks[name])), cost_rows, with_dollars=dollars)
            return {
                "definition_version": 1,
                "scope": selected_scope.response(),
                "sort": sort,
                "metric": metric,
                "period": page_grain.describe(),
                "caption": f"Last {len(page_grain.latest.periods)} completed {page_grain.kind}",
                "rows": rows,
                "findings": findings,
                "findings_state": derive_findings_state(rows, findings, agent_count),
                "overall": overall,
                "project_summaries": summaries["active"],
                "inactive_projects": summaries["inactive"],
                "breaks": breaks[metric],
            }
        finally:
            db.close()

    return await run_bulk(query)

@router.get("/models")
async def measurement_models(
    project: str = Query(...), agent: str = Query(...), grain: str = "days",
    timezone_name: str = Query(..., alias="timezone"), user: dict = Depends(require_user),
):
    """Return a one-row-per-model comparison for one composite scope and the page grain.

    The scope and the grain's windows come from the same primitives as ``/series``, so
    a Models share and the tokens panel below it read one vocabulary and one window. The
    share uses the same token expression the tokens family uses, in that family's own
    module, so a correction to what a token counts lands in both at once.

    Tasks, failed turns, token share and API-equivalent cost per accepted task read the
    grain's latest window (and the previous window for the change), with model identity
    and price from Cost's owners; see ``api.measurement_models``.
    """
    return await models_read(project, agent, resolve_grain(grain, timezone_name), user)


register_cost_route(router, require_user=require_user, resolve_grain=resolve_grain)

