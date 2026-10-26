"""Plan usage totals at Measurement's price.

``llm_usage.cost_usd`` is NULL for the rows a subscription harness or an
External endpoint writes, so a stored sum leaves a Claude Code or Codex task's
dollars unknown. Measurement prices those same rows when it reads them; this
module reads plan usage through that one path (its admission, input-tier and
idle-hours SQL, its identity resolution and ``classify_usage_rows``) and never
prices or holds a rate of its own.

Rows are grouped in SQL by the caller's key columns plus what classifies them,
never read per row. A group Measurement prices carries its dollars; every other
group (local, External without a listing, no list price, a row admission
refuses, an unproven context tier) keeps the stored sum and the count of rows
with no stored cost, exactly as before.
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


def _grouped_sql(keys: Sequence[str], where: str) -> str:
    """Calls grouped on what classifies them first; the context manifest is
    joined onto those few groups afterwards, not onto every call."""
    from api.cost_identity import IDENTITY_GROUP, identity_columns, identity_join
    from api.measurement_cost import ADMISSION_SQL, IDLE_HOURS_SQL, INPUT_TIER_SQL

    inner_keys = ",".join(f"u.{key} AS {key}" for key in keys)
    carried = ",".join(f"g.{key} AS {key}" for key in keys)
    plain = ",".join(keys)
    return f"""
        SELECT {plain},{IDENTITY_GROUP},admission,input_tier,idle_hours,
          SUM(records) AS records,{_OUTER_ADMITTED},
          SUM(tokens) AS tokens,SUM(unknown_tokens) AS unknown_tokens,
          SUM(cost) AS cost,SUM(unknown_cost) AS unknown_cost
        FROM (
          SELECT {carried},{identity_columns("g")},
            g.admission AS admission,g.input_tier AS input_tier,g.idle_hours AS idle_hours,
            g.records AS records,g.prompt_tokens AS prompt_tokens,g.completion_tokens AS completion_tokens,
            g.cache_creation_tokens AS cache_creation_tokens,g.cache_read_tokens AS cache_read_tokens,
            g.tokens AS tokens,g.unknown_tokens AS unknown_tokens,g.cost AS cost,g.unknown_cost AS unknown_cost
          FROM (
            SELECT {inner_keys},u.model AS model,u.context_manifest_id AS context_manifest_id,
              {ADMISSION_SQL} AS admission,{INPUT_TIER_SQL} AS input_tier,{IDLE_HOURS_SQL} AS idle_hours,
              COUNT(*) AS records,{_INNER_SUMS},
              SUM(u.total_tokens) AS tokens,SUM(u.total_tokens IS NULL) AS unknown_tokens,
              SUM(u.cost_usd) AS cost,SUM(u.cost_usd IS NULL) AS unknown_cost
            FROM llm_usage u WHERE u.{where} IN ({{marks}})
            GROUP BY {plain},u.model,u.context_manifest_id,admission,input_tier,idle_hours
          ) g {identity_join("g")}
        )
        GROUP BY {plain},{IDENTITY_GROUP},admission,input_tier,idle_hours
    """


def _add(left: Any, right: Any) -> Any:
    """Sum that stays NULL only while every addend is."""
    return right if left is None else left if right is None else left + right


def priced_usage(
    connection, keys: Sequence[str], read: Callable[[str], list],
) -> list[dict[str, Any]]:
    """One row per distinct ``keys`` value: ``tokens``, ``unknown_tokens``, ``cost``, ``unknown_cost``.

    ``keys[0]`` is the column the ids filter (``read`` receives the SQL with a
    single ``{marks}`` placeholder for them and returns its rows). ``cost`` and
    ``unknown_cost`` follow the module rule: Measurement's dollars for a group it
    prices, the stored sum and NULL count for any other.
    """
    from api.cost_identity import IdentityResolver
    from api.measurement_cost import classify_usage_rows, endpoint_listings

    shapes = [dict(row) for row in read(_grouped_sql(keys, keys[0]))]
    if not shapes:
        return []
    resolve = IdentityResolver(connection)
    for shape in shapes:
        shape["model"] = resolve(shape).key
    classified = classify_usage_rows(shapes, endpoint_listings(connection))
    totals: dict[tuple, dict[str, Any]] = defaultdict(
        lambda: {"tokens": None, "unknown_tokens": 0, "cost": None, "unknown_cost": 0}
    )
    for row in classified:
        total = totals[tuple(row[key] for key in keys)]
        total["tokens"] = _add(total["tokens"], row["tokens"])
        total["unknown_tokens"] += int(row["unknown_tokens"] or 0)
        if row["outcome"] == "priced":
            total["cost"] = _add(total["cost"], row["dollars"])
        else:
            total["cost"] = _add(total["cost"], row["cost"])
            total["unknown_cost"] += int(row["unknown_cost"] or 0)
    return [dict(zip(keys, key), **total) for key, total in totals.items()]
