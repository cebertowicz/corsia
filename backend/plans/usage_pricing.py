"""Plan usage totals at Measurement's price.

``llm_usage.cost_usd`` is NULL for the rows a subscription harness or an
External endpoint writes, so a stored sum leaves a Claude Code or Codex task's
dollars unknown. Measurement prices those same rows when it reads them; this
module reads plan usage through that one path (its admission, input-tier and
idle-hours SQL, its identity resolution and ``classify_usage_rows``) and never
prices or holds a rate of its own.

Two reads. The first is the plain per-key sum the plan reads always made:
tokens, the stored dollars and the count of rows with no stored cost. A stored
cost is a recorded charge (``llm.pricing.recorded_cost``: a cloud call's own
charge, or a local model's intentional 0) and is never repriced. Only when that
read finds rows with no stored cost does the second run, over those rows alone:
grouped in SQL by what classifies them (never read per row), then priced by
Measurement. Admission, input-tier and idle-hours SQL are evaluated per row, so
they cost the rows that need them and nobody else.

A group Measurement prices adds its dollars and leaves the unknown count; every
other group (local, External without a listing, no list price, a row admission
refuses, an unproven context tier) stays counted unknown, exactly as before.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from typing import Any

# ``api`` loads ``plans.public`` while it imports, so Measurement's pricing is loaded
# inside the functions that use it, never when this module loads (see
# ``tests/test_plans_public_boundary.py``).
_BUCKETS = ("prompt_tokens", "completion_tokens", "cache_creation_tokens", "cache_read_tokens")
_INNER_SUMS = ",".join(f"SUM(u.{bucket}) AS {bucket}" for bucket in _BUCKETS)
_OUTER_ADMITTED = ",".join(
    f"SUM(CASE WHEN admission='ok' THEN {bucket} ELSE 0 END) AS {bucket}" for bucket in _BUCKETS
)

#: Usage summed in SQL, so a read returns one row per group however many calls
#: the ledger holds. ``SUM`` is NULL when every value is: "no known amount".
USAGE_TOTALS = """SUM(total_tokens) AS tokens,SUM(total_tokens IS NULL) AS unknown_tokens,
                  SUM(cost_usd) AS cost,SUM(cost_usd IS NULL) AS unknown_cost"""


def _stored_sql(keys: Sequence[str]) -> str:
    plain = ",".join(keys)
    return (
        f"SELECT {plain},{USAGE_TOTALS} FROM llm_usage "
        f"WHERE {keys[0]} IN ({{marks}}) GROUP BY {plain}"
    )


def _unstored_sql(keys: Sequence[str]) -> str:
    """The rows with no stored cost, grouped on what classifies them; the context
    manifest is joined onto those few groups afterwards, not onto every call."""
    from api.cost_identity import IDENTITY_GROUP, identity_columns, identity_join
    from api.measurement_cost import ADMISSION_SQL, IDLE_HOURS_SQL, INPUT_TIER_SQL

    plain = ",".join(keys)
    inner_keys = ",".join(f"u.{key} AS {key}" for key in keys)
    carried = ",".join(f"g.{key} AS {key}" for key in keys)
    return f"""
        SELECT {plain},{IDENTITY_GROUP},admission,input_tier,idle_hours,
          SUM(records) AS records,{_OUTER_ADMITTED}
        FROM (
          SELECT {carried},{identity_columns("g")},
            g.admission AS admission,g.input_tier AS input_tier,g.idle_hours AS idle_hours,
            g.records AS records,g.prompt_tokens AS prompt_tokens,g.completion_tokens AS completion_tokens,
            g.cache_creation_tokens AS cache_creation_tokens,g.cache_read_tokens AS cache_read_tokens
          FROM (
            SELECT {inner_keys},u.model AS model,u.context_manifest_id AS context_manifest_id,
              {ADMISSION_SQL} AS admission,{INPUT_TIER_SQL} AS input_tier,{IDLE_HOURS_SQL} AS idle_hours,
              COUNT(*) AS records,{_INNER_SUMS}
            FROM llm_usage u WHERE u.{keys[0]} IN ({{marks}}) AND u.cost_usd IS NULL
            GROUP BY {plain},u.model,u.context_manifest_id,admission,input_tier,idle_hours
          ) g {identity_join("g")}
        )
        GROUP BY {plain},{IDENTITY_GROUP},admission,input_tier,idle_hours
    """


def _priced(
    connection, keys: Sequence[str], rows: list[dict[str, Any]],
) -> dict[tuple, dict[str, Any]]:
    """Measurement's dollars and priced-row count per key, for the rows it can price."""
    from api.cost_identity import IdentityResolver
    from api.measurement_cost import classify_usage_rows, endpoint_listings

    resolve = IdentityResolver(connection)
    for row in rows:
        row["model"] = resolve(row).key
    found: dict[tuple, dict[str, Any]] = defaultdict(lambda: {"dollars": 0.0, "records": 0})
    for row in classify_usage_rows(rows, endpoint_listings(connection)):
        if row["outcome"] == "priced":
            total = found[tuple(row[key] for key in keys)]
            total["dollars"] += row["dollars"]
            total["records"] += int(row["records"])
    return found


def priced_usage(
    connection, keys: Sequence[str], ids: Sequence[str], read: Callable[[str, Sequence[str]], list],
) -> list[dict[str, Any]]:
    """One row per distinct ``keys`` value: ``tokens``, ``unknown_tokens``, ``cost``, ``unknown_cost``.

    ``keys[0]`` is the column ``ids`` filter; ``read(sql, ids)`` runs ``sql``, which
    carries one ``{marks}`` placeholder for the ids, and returns its rows.
    """
    totals = [dict(row) for row in read(_stored_sql(keys), ids)]
    unstored = sorted({str(row[keys[0]]) for row in totals if row["unknown_cost"]})
    if not unstored:
        return totals
    found = _priced(connection, keys, [dict(row) for row in read(_unstored_sql(keys), unstored)])
    for row in totals:
        price = found.get(tuple(row[key] for key in keys))
        if price is not None:
            row["cost"] = (row["cost"] or 0) + price["dollars"]
            row["unknown_cost"] -= price["records"]
    return totals
