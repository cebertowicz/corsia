"""Plan usage totals at API price, through Measurement's pricing path.

``llm_usage.cost_usd`` is NULL for the rows a subscription harness or an
External endpoint writes, so a stored sum leaves a Claude Code or Codex task's
dollars unknown. Measurement prices those rows when it reads them, at the
provider's API list price (never a charge a subscription made); this module
reads plan usage through that one path (its admission, input-tier and
idle-hours SQL, its identity resolution and ``classify_usage_rows``) and never
prices or holds a rate of its own.

A plan's dollars are therefore of two kinds, summed into one figure at API price:
the recorded charge of a row that has one, and Measurement's API-equivalent price
for a row that has none. Measurement itself reprices every admitted row and
ignores a stored charge, so the two figures agree only for rows stored NULL.

Two reads. The first is the plain per-key sum the plan reads always made:
tokens, the stored dollars and the count of rows with no stored cost. A stored
cost is a recorded charge (``llm.pricing.recorded_cost``: an API-billed call's own
charge, or a local model's intentional 0) and is never repriced. Only when that
read finds rows with no stored cost does the second run, over those rows alone:
grouped in SQL by what classifies them (never read per row), then priced by
Measurement. Admission, input-tier and idle-hours SQL are evaluated per row, so
they cost the rows that need them and nobody else.

A group Measurement prices adds its dollars and leaves the unknown count; every
other group (local, External without a listing, no list price, a row admission
refuses, an unproven context tier) stays counted unknown, exactly as before.

Only a read of the plan's cost reaches this module (``plans.operator_node_facts``,
and the exported ``commitment_cost.project_many``); a plan write and the status it
journals read no cost (#2489, ``tests/test_plan_write_cost.py``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cached_property
from typing import Any

from core.measurement_token_contract import TOKEN_BUCKETS

# ``api`` loads ``plans.public`` while it imports, so Measurement's pricing is loaded
# inside the functions that use it, never when this module loads (see
# ``tests/test_plans_public_boundary.py``).
_INNER_SUMS = ",".join(f"SUM(u.{bucket}) AS {bucket}" for bucket in TOKEN_BUCKETS)
_OUTER_ADMITTED = ",".join(
    f"SUM(CASE WHEN admission='ok' THEN {bucket} ELSE 0 END) AS {bucket}" for bucket in TOKEN_BUCKETS
)

#: Usage summed in SQL, so a read returns one row per group however many calls
#: the ledger holds. ``SUM`` is NULL when every value is: "no known amount".
USAGE_TOTALS = """SUM(total_tokens) AS tokens,SUM(total_tokens IS NULL) AS unknown_tokens,
                  SUM(cost_usd) AS cost,SUM(cost_usd IS NULL) AS unknown_cost"""


class _Pricing:
    """Measurement's identity resolution and endpoint listings over one connection, each built once."""

    def __init__(self, connection) -> None:
        self.connection = connection

    @cached_property
    def resolve(self):
        from api.cost_identity import IdentityResolver

        return IdentityResolver(self.connection)

    @cached_property
    def listings(self) -> dict[str, str]:
        from api.measurement_cost import endpoint_listings

        return endpoint_listings(self.connection)


_SHARED: ContextVar[_Pricing | None] = ContextVar("plan_usage_pricing", default=None)


@contextmanager
def one_read(connection) -> Iterator[None]:
    """Share the identity resolver and endpoint listings among the pricing calls of one read.

    A read of a plan's cost prices twice (the commitment totals and the node totals);
    inside this block both build them once, on first use, instead of once each.
    """
    if _SHARED.get() is not None:
        yield
        return
    token = _SHARED.set(_Pricing(connection))
    try:
        yield
    finally:
        _SHARED.reset(token)


def _pricing(connection) -> _Pricing:
    shared = _SHARED.get()
    return shared if shared is not None and shared.connection is connection else _Pricing(connection)


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
    from api.measurement_cost import classify_usage_rows

    pricing = _pricing(connection)
    for row in rows:
        row["model"] = pricing.resolve(row).key
    found: dict[tuple, dict[str, Any]] = defaultdict(lambda: {"dollars": 0.0, "records": 0})
    for row in classify_usage_rows(rows, pricing.listings):
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
