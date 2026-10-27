"""Actual cost of External endpoint usage, classified at read time.

The ledger stores no charge for an External call (its ``cost_usd`` is NULL, or a
legacy placeholder zero), so what the user recorded about the endpoint decides
how its usage counts. History is never rewritten: each call is classified by the
endpoint's mode on the day of the call, from its dated billing facts.

1. a ``subscription`` day is subscription usage; its charge is the endpoint's
   charge dates, counted with the accounts' (``connections.subscription_billing``);
2. a ``pay_per_use`` day is a direct charge, priced at read time at the public
   list rate of the endpoint's own host when that host lists the model, else an
   unknown charge;
3. a day with no mode recorded is an unknown charge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from connections.billing_prices import mode_on, price_facts_by_payer
from core.measurement_pricing import ADMISSION_SQL, IDLE_HOURS_SQL, INPUT_TIER_SQL, classify_usage_rows, endpoint_listings
from llm.endpoint_billing import server_id_sql


@dataclass
class ExternalActual:
    """The External share of Actual: records by class and the priced dollars."""

    subscription_records: int = 0
    unknown_charge_records: int = 0
    unset_records: int = 0
    direct_dollars: float = 0.0


def _groups(db, bounded: dict[str, Any]) -> list[dict[str, Any]]:
    """External ledger rows grouped by endpoint, day and priceable shape."""
    return [dict(row) for row in db.execute(
        f"""
        SELECT server_id, day, model, admission, input_tier, idle_hours, COUNT(*) AS records,
          SUM(CASE WHEN admission='ok' THEN prompt_tokens ELSE 0 END) AS prompt_tokens,
          SUM(CASE WHEN admission='ok' THEN completion_tokens ELSE 0 END) AS completion_tokens,
          SUM(CASE WHEN admission='ok' THEN cache_creation_tokens ELSE 0 END) AS cache_creation_tokens,
          SUM(CASE WHEN admission='ok' THEN cache_read_tokens ELSE 0 END) AS cache_read_tokens
        FROM (
          SELECT {server_id_sql('u.model')} AS server_id, substr(u.created_at,1,10) AS day, u.model AS model,
            {ADMISSION_SQL} AS admission, {INPUT_TIER_SQL} AS input_tier, {IDLE_HOURS_SQL} AS idle_hours,
            u.prompt_tokens, u.completion_tokens, u.cache_creation_tokens, u.cache_read_tokens
          FROM llm_usage u
          WHERE u.thread_id IN (SELECT id FROM measurement_scope_threads)
            AND u.created_at >= :from_ts AND u.created_at < :to_ts AND u.model >= 'external@' AND u.model < 'external@~'
            AND {bounded['actual_window']}
        )
        GROUP BY server_id, day, model, admission, input_tier, idle_hours
        """,
        bounded,
    )]


def external_actual(db, bounded: dict[str, Any]) -> ExternalActual:
    """Classify the scope's External usage in the window; the caller adds it to Actual."""
    groups = _groups(db, bounded)
    result = ExternalActual()
    if not groups:
        return result
    facts, listings = price_facts_by_payer(db, "network_server_id"), endpoint_listings(db)
    priced: list[dict[str, Any]] = []
    for group in groups:
        server_id, records = str(group["server_id"]), int(group["records"])
        mode = mode_on(facts.get(server_id, []), date.fromisoformat(group["day"]))
        if mode == "subscription":
            result.subscription_records += records
        elif mode == "pay_per_use":
            priced.append(group)
        else:
            result.unknown_charge_records += records
            result.unset_records += records
    for group in classify_usage_rows(priced, listings):
        if group["outcome"] == "priced":
            result.direct_dollars += float(group["dollars"])
        else:
            result.unknown_charge_records += int(group["records"])
    return result
