"""Measurement's row pricing: which ledger rows may be priced, and at what API-equivalent price.

The admission, input-tier and idle-hours SQL classify each ``llm_usage`` row;
``grouped_usage_sql`` groups a row source on what classifies it; ``classify_usage_rows``
prices each group with ``llm.api_equivalent`` or names why it has no price. Measurement's
Cost, Models and External actual reads and the plan cost reads all price through here, so
one row has one API-equivalent price wherever it is shown. Nothing here reads or writes
``llm_usage.cost_usd``, the call-time charge Spend shows.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.cost_identity import IDENTITY_GROUP, identity_columns, identity_join
from core.measurement_token_contract import TOKEN_BUCKETS, buckets_reported_sql, contract_proven_sql, total_agrees_sql
from llm.api_equivalent import (
    api_equivalent, has_public_long_context_price, has_unpublished_cache_rate, is_not_api_priced, price_identity,
)
from llm.list_prices import LIST_PRICES, OPENAI_LONG_CONTEXT_INPUT_TOKENS, listing_for_host
from llm.network_endpoints import is_cloud_server

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


_ADMITTED = ",".join(f"SUM(CASE WHEN admission='ok' THEN {bucket} ELSE 0 END) AS {bucket}" for bucket in TOKEN_BUCKETS)


def grouped_usage_sql(keys: Mapping[str, str], source: str) -> str:
    """The grouped, classifiable usage read over ``source``: the shape ``classify_usage_rows`` prices.

    One row per ``keys`` value and classifying shape (``IDENTITY_GROUP``, admission, input tier,
    idle hours), with its retained-record count and admitted token sums. ``keys`` maps each output
    column to its expression; ``source`` is the FROM clause, with any join or WHERE, whose
    ``llm_usage`` alias is ``u``. Calls are grouped on what classifies them first, and the context
    manifest is joined afterwards, onto those few groups instead of onto every call.
    """
    names = ",".join(keys)
    expressions = ",".join(keys.values())
    selected = ",".join(f"{expression} AS {name}" for name, expression in keys.items())
    carried = ",".join(f"g.{name} AS {name}" for name in keys)
    classes = ",".join(f"g.{name} AS {name}" for name in ("admission", "input_tier", "idle_hours", "records", *TOKEN_BUCKETS))
    sums = ",".join(f"SUM(u.{bucket}) AS {bucket}" for bucket in TOKEN_BUCKETS)
    return f"""
        SELECT {names},{IDENTITY_GROUP},admission,input_tier,idle_hours,SUM(records) AS records,{_ADMITTED}
        FROM (
          SELECT {carried},{identity_columns("g")},{classes}
          FROM (
            SELECT {selected},u.model AS model,u.context_manifest_id AS context_manifest_id,
              {ADMISSION_SQL} AS admission,{INPUT_TIER_SQL} AS input_tier,{IDLE_HOURS_SQL} AS idle_hours,
              COUNT(*) AS records,{sums}
            FROM {source}
            GROUP BY {expressions},u.model,u.context_manifest_id,admission,input_tier,idle_hours
          ) g {identity_join("g")}
        )
        GROUP BY {names},{IDENTITY_GROUP},admission,input_tier,idle_hours
    """


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
