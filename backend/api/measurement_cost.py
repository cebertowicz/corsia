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

from api.cost_identity import IDENTITY_GROUP, IdentityResolver, identity_columns, identity_join
from api.measurement_actual import build_actual
from api.measurement_average import cost_average
from api.measurement_breaks import persist_source_starts, source_started_period
from api.measurement_join import joined_read
from api.measurement_queries import (
    PREPARED_OUTCOMES, PREPARED_SCOPE, prepare_outcome_periods, prepare_scope_threads,
)
from api.measurement_registry import REGISTRY
from api.measurement_scope import validate_scope
from api.measurement_size import size_period_totals
from api.usage_rollup import usage_params, usage_source
from core.measurement_scope import Grain, Window, query_params
from core.measurement_token_contract import (
    CLAUDE_CODE_SOURCE, CODEX_SOURCE, TOKEN_BUCKETS, buckets_reported_sql, contract_proven_sql, total_agrees_sql,
)
from infra.database import get_db
from infra.db_async import run_bulk, run_in_db
from llm.api_equivalent import (
    api_equivalent, has_public_long_context_price, has_unpublished_cache_rate, is_not_api_priced, price_identity,
)
from llm.list_prices import LIST_PRICES, OPENAI_LONG_CONTEXT_INPUT_TOKENS, listing_for_host
from llm.network_endpoints import is_cloud_server

# The size family's minimum n: the same rule that withholds Work per hour of your attention.
MINIMUM_SIZED = REGISTRY["estimated_human_hours_per_active_hour"].minimum_n
# OpenAI's public Standard boundary is based on an individual call's input,
# not its output or a period's aggregate. A row within the short bound is safe
# even if it combines usage records; above it, opaque Codex receipts cannot prove tier.
INPUT_TIER_SQL = (
    f"CASE WHEN u.prompt_tokens+u.cache_creation_tokens+u.cache_read_tokens>{OPENAI_LONG_CONTEXT_INPUT_TOKENS} "
    "THEN 'above_short_bound' ELSE 'short_bound' END"
)
# Providers with a published idle-hours vector bill by the call's own time, so
# the hour is read per row, in UTC, before aggregation, against the window of
# the price entries for the External identity the row served. The row's list is
# resolved later, so every list carrying an identity must share its window.
_UTC_HOUR = "CAST(substr(u.created_at,12,2) AS INTEGER)"
_IDLE_WINDOWS = {price.model: price.idle_utc_hours for price in LIST_PRICES if price.idle_utc_hours is not None}
if any(price.idle_utc_hours not in (None, _IDLE_WINDOWS.get(price.model)) for price in LIST_PRICES):
    raise ValueError("one identity has different idle-hours windows on different lists")
_IDLE_CASES = " ".join(
    f"WHEN COALESCE(u.model,'') LIKE 'external@%' AND substr(u.model,instr(u.model,'/')+1)='{model}' "
    f"AND {_UTC_HOUR}>={start} AND {_UTC_HOUR}<{end} THEN 1"
    for model, (start, end) in _IDLE_WINDOWS.items()
)
IDLE_HOURS_SQL = f"CASE {_IDLE_CASES} ELSE 0 END" if _IDLE_CASES else "0"

# Which rows may be priced. A row is priceable only when every disjoint bucket was
# reported (typeof proves non-NULL, whole and not a blob) and any recorded total
# agrees with them. Cache tokens additionally need the writer's token contract:
# the ledger keeps no per-row contract marker, so the recorded source is the only
# evidence, and it must agree with the model's harness prefix. Claude Code rows
# were always written disjoint (#1215). Two more Claude Code writers have kept
# Anthropic's disjoint buckets since their first ledger row: source 'checkpoint'
# began with #1764 (2026-09-15), whose Claude Code rows are
# ``from_anthropic(...).ledger_row()`` in ``core.fork_cc_io``; the
# host-access reviewer has copied the CLI's input, cache-creation and cache-read
# counts field for field since its first row (2026-07-21,
# ``llm.isolated_structured._capture_claude_telemetry``). Codex rows before the 2026-08-20
# disjoint contract (#1056) may be thread-cumulative or cache-inclusive,
# which neither a zero cache nor a matching total disproves. A Codex row is
# priced only when its id is a 24-hex receipt id: ``_receipt_id`` (turn usage,
# #1626, 2026-09-11) and the checkpoint writer (#1764, 2026-09-15) are the
# only writers of that shape, both later than the contract. The checkpoint Codex
# path is per-call and disjoint: ``subagents.codex_app_server_notifications.record_usage``
# emits ``CumulativeTurnVolume.observe`` deltas as ``TokenUsage.ledger_row()``, and
# ``core.fork_codex_result.CodexForkCapture.publish_calls`` writes each
# one. Direct writes use ``uuid4()[:12]`` (hyphenated, so never 24 hex). The
# host-access reviewer's Codex rows always recorded cache creation NULL, as
# Codex reports no cache-creation count (``infra.provider_usage.from_codex``),
# so those rows stop at 'tokens_unreported' above; a reviewer Codex row with
# every bucket reported is written by ``from_codex``. An External conversation row
# is disjoint from 2026-09-17: the native path normalizes OpenAI-compatible
# usage into disjoint buckets in ``infra.provider_usage.from_litellm``, present in
# this repository's first commit, and the later rows carry cache reads larger
# than their prompt, impossible in the older cache-inclusive shape. Anything else
# prices only without cache, where the disjoint and cache-inclusive contracts agree.
# Evaluated as an explicit 0/1 so a NULL source or model can never reach
# ``NOT NULL`` (which is NULL, not true) and fall through to 'ok'. These are
# the same alias-aware clauses the partial source-start index uses.
_BUCKETS_REPORTED = buckets_reported_sql("u")
_TOTAL_AGREES = total_agrees_sql("u")
_CONTRACT_PROVEN = contract_proven_sql("u")
ADMISSION_SQL = (
    f"CASE WHEN NOT ({_BUCKETS_REPORTED}) THEN 'tokens_unreported' "
    f"WHEN NOT {_TOTAL_AGREES} THEN 'tokens_inconsistent' "
    f"WHEN NOT {_CONTRACT_PROVEN} THEN 'contract_unproven' ELSE 'ok' END"
)
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

    def query() -> tuple[dict[str, Any], dict[str, str | None]]:
        db = get_db()
        try:
            selected_scope = validate_scope(db, user["id"], project, agent)
            params = {**selected_scope.params(user["id"]), "grain": page_grain}
            prepare_outcome_periods(db, params)
            pending_starts: dict[str, str | None] = {}
            cost = build_cost(db, params, ledger, pending_starts=pending_starts)
            # The series' moving average spans the grain's own window, served so the chart names it without a copy.
            return ({"scope": selected_scope.response(), "period": page_grain.describe(),
                     "average_periods": page_grain.average_periods, **cost}, pending_starts)
        finally:
            db.close()

    result, pending_starts = await run_bulk(query)
    if any(pending_starts.values()):
        await run_in_db(persist_source_starts, pending_starts)
    return result


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


def endpoint_listings(db) -> dict[str, str]:
    """The regional price list of each External server whose public endpoint is a listed cloud host."""
    servers: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for row in db.execute(
        "SELECT s.id, s.kind, e.host, e.kind AS endpoint_kind FROM network_servers s "
        "JOIN network_server_endpoints e ON e.server_id = s.id WHERE s.kind = 'external'"
    ).fetchall():
        servers.setdefault(str(row["id"]), (str(row["kind"]), []))[1].append(
            {"host": str(row["host"]), "kind": row["endpoint_kind"]})
    listings: dict[str, str] = {}
    for server_id, (kind, endpoints) in servers.items():
        found = {listing_for_host(e["host"]) for e in endpoints if e["kind"] == "public"} - {None}
        if is_cloud_server(kind, endpoints) and len(found) == 1:
            listings[server_id] = str(found.pop())
    return listings


def _listing(model: str, listings: Mapping[str, str]) -> str | None:
    harness = model.partition("/")[0]
    return listings.get(harness.removeprefix("external@")) if harness.startswith("external@") else None


def classify_usage_rows(rows: list[dict[str, Any]], listings: Mapping[str, str]) -> list[dict[str, Any]]:
    """Attach dollars, or the reason there are none, to each grouped ledger shape.

    ``listings`` is ``endpoint_listings``: an External row prices only when its
    server is a listed cloud endpoint whose regional list carries its served identity.
    """
    classified: list[dict[str, Any]] = []
    for row in rows:
        model = str(row["model"] or "")
        listing = _listing(model, listings)
        if is_not_api_priced(model, listing):
            classified.append({**row, "outcome": "not_api_priced", "dollars": None})
            continue
        reason = "no_list_price" if price_identity(model, listing) is None else (
            None if row["admission"] == "ok" else row["admission"])
        if (reason is None and row["input_tier"] == "above_short_bound"
                and has_public_long_context_price(model, listing)):
            reason = "context_tier_unproven"
        idle_hours = bool(row.get("idle_hours"))
        buckets = tuple(int(row[bucket]) for bucket in TOKEN_BUCKETS)
        dollars = None if reason else api_equivalent(model, *buckets, idle_hours=idle_hours, listing=listing)
        if dollars is None and reason is None:
            reason = ("cache_rate_unpublished" if has_unpublished_cache_rate(
                model, buckets[2], buckets[3], idle_hours=idle_hours, listing=listing) else "no_list_price")
        classified.append({
            **row, "outcome": reason or "priced", "dollars": dollars,
        })
    return classified


def _series(db, classified: list[dict[str, Any]], sized: Mapping[str, Mapping[str, Any]], grain: Grain,
            *, pending_starts: dict[str, str | None] | None = None) -> list[dict[str, Any]]:
    """Weekly known subtotal and retained-usage-record coverage; Unknown only when nothing was priceable.

    ``sized`` is the week's size-rated accepted outcomes and their hours of work. All priced
    spend in a period divided by those hours is the all-in cost per hour of delivered work; it
    is withheld, never zero, when fewer than ``MINIMUM_SIZED`` outcomes stand behind it.
    The series starts at its first priced period. Once size ratings exist, it also waits for
    their install-wide instrument start before serving Work per hour, so no leading period
    reads as no data while a spend-only subtotal remains observable.
    """
    size_start = source_started_period(db, "estimated_human_hours_per_active_hour", grain, pending_starts=pending_starts)
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


def build_cost(db, params: dict[str, Any], ledger: list[dict[str, Any]] | None = None,
               *, pending_starts: dict[str, str | None] | None = None) -> dict[str, Any]:
    """Build Cost from Measurement's already prepared scope and outcome periods.

    ``ledger`` is the grain's classified usage ledger as ``ledger_read`` shares it; without one it is read here.

    ``latest`` and ``previous`` are the grain's two comparison windows (7 days against the
    7 before, or 4 weeks against the 4 before), pooled; ``series`` is every completed period.
    """
    grain: Grain = params["grain"]
    classified = ledger if ledger is not None else ledger_shapes(db, params)
    every = _series(db, classified, size_period_totals(db, params), grain, pending_starts=pending_starts)
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
