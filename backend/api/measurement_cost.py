"""Read-only API-equivalent Cost calculations for Measurement.

Prices the retained token ledger at read time with ``llm.api_equivalent``. It
never changes ``llm_usage.cost_usd``: that remains the call-time value owned by
``llm.pricing`` and displayed by Spend. The dollars served are a known subtotal
with explicit retained-usage-record coverage, never a total that hides unpriced records.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from fastapi import Depends, Query

from api.measurement_actual import build_actual
from api.measurement_average import cost_average
from api.measurement_breaks import source_started_period
from api.measurement_join import joined_read
from api.measurement_queries import (
    PREPARED_OUTCOMES, PREPARED_SCOPE, prepare_outcome_periods, prepare_scope_threads,
)
from api.measurement_registry import REGISTRY
from api.measurement_scope import validate_scope
from api.measurement_size import size_period_totals
from api.usage_rollup import usage_params, usage_source
from core.cost_identity import IDENTITY_GROUP, IdentityResolver, identity_columns, identity_join
from core.measurement_pricing import classify_usage_rows, endpoint_listings
from core.measurement_scope import Grain, Window, query_params
from infra.database import get_db
from infra.db_async import run_bulk

# The size family's minimum n: the same rule that withholds Work per hour of your attention.
MINIMUM_SIZED = REGISTRY["estimated_human_hours_per_active_hour"].minimum_n

_EXCLUDED_TEXT = {
    "no_list_price": "usage records without a maintained rate for their recorded model identity",
    "cache_rate_unpublished": "usage records whose provider publishes no rate for their cached tokens",
    "context_tier_unproven": "usage records whose API context tier is not proven",
    "tokens_unreported": "usage records with unreported token counts",
    "tokens_inconsistent": "usage records whose recorded total disagrees with their token counts",
    "contract_unproven": "usage records whose token counting format is not proven",
}


@joined_read
async def ledger_read(project: str, agent: str, page_grain: Grain, user: dict) -> list[dict[str, Any]]:
    """The scope's classified usage ledger over the averaged page grain, joined with an identical read in flight.

    Cost and Models both price this one ledger, so a page load that draws both reads it once. A reader
    keeps the periods it needs (Models its latest window) and never mutates the rows.
    """
    def query() -> list[dict[str, Any]]:
        db = get_db()
        try:
            selected_scope = validate_scope(db, user["id"], project, agent)
            params = {**selected_scope.params(user["id"]), "grain": page_grain}
            prepare_scope_threads(db, params)
            return ledger_shapes(db, params)
        finally:
            db.close()

    return await run_bulk(query)


@joined_read
async def cost_read(project: str, agent: str, page_grain: Grain, user: dict) -> dict[str, Any]:
    """The Cost payload for one viewer, scope and calendar, joined with an identical read in flight.

    The page's Cost panel and ``/where-to-look`` (which ranks the Cost headline's $ per hour of
    work) both read it, and it prices the ledger ``ledger_read`` shares with Models, so one page
    load reads the ledger once.
    """
    ledger = await ledger_read(project, agent, page_grain, user)

    def query() -> dict[str, Any]:
        db = get_db()
        try:
            selected_scope = validate_scope(db, user["id"], project, agent)
            params = {**selected_scope.params(user["id"]), "grain": page_grain}
            prepare_outcome_periods(db, params)
            cost = build_cost(db, params, ledger)
            # The series' moving average spans the grain's own window, served so the chart names it without a copy.
            return {"scope": selected_scope.response(), "period": page_grain.describe(),
                    "average_periods": page_grain.average_periods, **cost}
        finally:
            db.close()

    return await run_bulk(query)


def register_cost_route(router, *, require_user, resolve_grain) -> None:
    """Attach Cost to the shared Measurement router without growing its module."""
    @router.get("/cost")
    async def measurement_cost(
        project: str = Query(...), agent: str = Query(...), grain: str = "days",
        timezone_name: str = Query(..., alias="timezone"), user: dict = Depends(require_user),
    ):
        """Return the known API-equivalent subtotal with retained-usage-record coverage and Actual completeness."""
        return await cost_read(project, agent, resolve_grain(grain, timezone_name, averaged=True), user)


def _usage_rows(db, params: dict[str, Any]) -> list[dict[str, Any]]:
    """One grouped ledger read. Counts are retained usage records; token sums include only admitted rows.

    Each shape carries the manifest identity its turn ran under, so ``model`` is the resolved identity
    (the key Models and Spend price by), never the raw selector an alias row records.
    """
    bounded = usage_params(db, params)
    rows = db.execute(
        f"""
        SELECT {bounded['grain'].bucket('u.hour')} AS period, {identity_columns('u')},
          u.admission AS admission, u.input_tier AS input_tier, u.idle_hours AS idle_hours,
          SUM(u.records) AS records,
          SUM(CASE WHEN u.admission='ok' THEN u.prompt_tokens ELSE 0 END) AS prompt_tokens,
          SUM(CASE WHEN u.admission='ok' THEN u.completion_tokens ELSE 0 END) AS completion_tokens,
          SUM(CASE WHEN u.admission='ok' THEN u.cache_creation_tokens ELSE 0 END) AS cache_creation_tokens,
          SUM(CASE WHEN u.admission='ok' THEN u.cache_read_tokens ELSE 0 END) AS cache_read_tokens
        FROM {usage_source(PREPARED_SCOPE, bounded['grain'])} u {identity_join('u')}
        WHERE {bounded['grain'].bucket('u.hour')} IS NOT NULL
        GROUP BY period, {IDENTITY_GROUP}, u.admission, u.input_tier, u.idle_hours
        ORDER BY period, model, u.input_tier, u.idle_hours
        """,
        bounded,
    ).fetchall()
    resolve = IdentityResolver(db)
    shapes = [dict(row) for row in rows]
    for shape in shapes:
        identity = resolve(shape)
        shape["model"], shape["model_label"] = identity.key, identity.label
    return shapes


def ledger_shapes(db, params: dict[str, Any]) -> list[dict[str, Any]]:
    """The grouped ledger of the prepared periods, each shape priced or told why not: the one read Cost and Models share."""
    return classify_usage_rows(_usage_rows(db, params), endpoint_listings(db))


def _series(db, classified: list[dict[str, Any]], sized: Mapping[str, Mapping[str, Any]], grain: Grain) -> list[dict[str, Any]]:
    """Weekly known subtotal and retained-usage-record coverage; Unknown only when nothing was priceable.

    ``sized`` is the week's size-rated accepted outcomes and their hours of work. All priced
    spend in a period divided by those hours is the all-in cost per hour of delivered work; it
    is withheld, never zero, when fewer than ``MINIMUM_SIZED`` outcomes stand behind it.
    The series starts at its first priced period. Work per hour also waits for the first
    accepted outcome with a size rating, so no leading period reads as no data while a
    spend-only subtotal remains observable.
    """
    size_start = source_started_period(db, "estimated_human_hours_per_active_hour", grain)
    outcomes = {row["period"]: int(row["outcomes"]) for row in db.execute(
        f"SELECT period,outcomes FROM {PREPARED_OUTCOMES}"
    ).fetchall()}
    by_period: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in classified:
        by_period[str(row["period"])].append(row)
    first_priced = next(
        (period for period in sorted(by_period) if any(row["outcome"] == "priced" for row in by_period[period])), None)
    if first_priced is None:
        return []
    series: list[dict[str, Any]] = []
    for period in sorted(set(outcomes) | set(by_period)):
        if period < first_priced or (size_start is not None and period < size_start):
            continue
        rows = by_period[period]
        priced = [row for row in rows if row["outcome"] == "priced"]
        dollars = sum(float(row["dollars"]) for row in priced) if priced else None
        outcomes_count = outcomes.get(period, 0)
        period_sized = int((sized.get(period) or {}).get("usable", 0))
        hours = float((sized.get(period) or {}).get("estimated_hours", 0.0))
        series.append({
            "period": period,
            "dollars": round(dollars, 10) if dollars is not None else None,
            "priced_records": sum(int(row["records"]) for row in priced),
            "usage_records": sum(int(row["records"]) for row in rows),
            "outcomes": outcomes_count,
            "per_outcome": round(dollars / outcomes_count, 10) if dollars is not None and outcomes_count else None,
            "sized": period_sized,
            "hours": hours,
            "per_hour": round(dollars / hours, 10) if dollars is not None and hours and period_sized >= MINIMUM_SIZED else None,
        })
    return series


def _pooled(series: list[dict[str, Any]], window: Window, grain: Grain) -> dict[str, Any]:
    """One comparison window's figures, pooled from its periods' rows.

    The rates are the window's own totals divided, never an average of period rates, so a
    quiet day does not weigh as much as a busy one. ``None`` when no period was priced.
    """
    rows = [row for row in series if row["period"] in window.periods]
    priced = [float(row["dollars"]) for row in rows if row["dollars"] is not None]
    dollars = sum(priced) if priced else None
    outcomes = sum(int(row["outcomes"]) for row in rows)
    sized = sum(int(row["sized"]) for row in rows)
    hours = sum(float(row["hours"]) for row in rows)
    return {
        "from": window.first, "to": grain.period_end(window.last),
        "dollars": round(dollars, 10) if dollars is not None else None,
        "priced_records": sum(int(row["priced_records"]) for row in rows),
        "usage_records": sum(int(row["usage_records"]) for row in rows),
        "outcomes": outcomes, "sized": sized, "hours": hours,
        "per_outcome": round(dollars / outcomes, 10) if dollars is not None and outcomes else None,
        "per_hour": round(dollars / hours, 10) if dollars is not None and hours and sized >= MINIMUM_SIZED else None,
    }


def _excluded_models(classified: list[dict[str, Any]], grain: Grain) -> dict[str, list[dict[str, Any]]]:
    """Name each latest-window exclusion by the label of the resolved identity it retained."""
    grouped: dict[str, dict[str, int]] = {}
    for row in classified:
        kind = str(row["outcome"])
        if kind not in _EXCLUDED_TEXT or str(row["period"]) not in grain.latest.periods:
            continue
        models = grouped.setdefault(kind, {})
        label = str(row["model_label"])
        models[label] = models.get(label, 0) + int(row["records"])
    return {
        kind: [
            {"label": label, "records": records}
            for label, records in sorted(models.items(), key=lambda item: (-item[1], item[0]))
        ]
        for kind, models in grouped.items()
    }


def build_cost(db, params: dict[str, Any], ledger: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build Cost from Measurement's already prepared scope and outcome periods.

    ``ledger`` is the grain's classified usage ledger as ``ledger_read`` shares it; without one it is read here.

    ``latest`` and ``previous`` are the grain's two comparison windows (7 days against the
    7 before, or 4 weeks against the 4 before), pooled; ``series`` is every completed period.
    """
    grain: Grain = params["grain"]
    classified = ledger if ledger is not None else ledger_shapes(db, params)
    every = _series(db, classified, size_period_totals(db, params), grain)
    # The lead-in periods are read only so each drawn period has a full span for its average.
    series = [{**row, "average": cost_average(every, grain, row["period"], MINIMUM_SIZED)} for row in every if row["period"] in grain.served]
    # Coverage names the headline window, the one the card's figures and caption name, so neither the
    # drawn range's earlier periods nor the averaging lead-in's records are counted.
    records: dict[str, int] = defaultdict(int)
    for row in classified:
        if str(row["period"]) in grain.latest.periods:
            records[row["outcome"]] += int(row["records"])
    excluded_models = _excluded_models(classified, grain)
    return {
        "definition_version": 1,
        "series": series,
        "latest": _pooled(every, grain.latest, grain) if every else None,
        "previous": _pooled(every, grain.previous, grain) if every else None,
        "coverage": {"priced_records": records["priced"], "usage_records": sum(records.values())},
        "not_api_priced_records": records["not_api_priced"],
        "excluded": [
            {"kind": kind, "text": text, "records": records[kind], "models": excluded_models[kind]}
            for kind, text in _EXCLUDED_TEXT.items() if records[kind]
        ],
        "actual": build_actual(db, params),
    }
