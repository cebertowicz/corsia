"""The usage ledger's tokens and dollars for plan nodes and commitments.

Tokens are summed from ``llm_usage``. Dollars are Measurement's API-equivalent price of
the same rows, through ``core.measurement_pricing``: the grouped read, admission and
``classify_usage_rows`` that Measurement's Cost and Models read. A row's stored
``cost_usd`` is not read.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping

from core.cost_identity import IdentityResolver
from core.measurement_pricing import classify_usage_rows, endpoint_listings, grouped_usage_sql
from infra.packed_rows import fetch_all

#: Tokens summed in SQL, so a read returns one row per group however many calls
#: the ledger holds. ``SUM`` is NULL when every value is: "no known amount".
USAGE_TOKENS = "SUM(total_tokens) AS tokens,SUM(total_tokens IS NULL) AS unknown_tokens"


def usage_dollars(connection, ids: Mapping[str, Collection[str]]) -> dict[str, dict[str, dict]]:
    """Each id's ``model_cost_usd``: Measurement's API-equivalent price of its ``llm_usage`` rows.

    ``ids`` maps a ledger column (``plan_node_id``, ``plan_commitment_id``) to the ids it names;
    the result maps each column to ``{id: amount}``. All columns are priced in one grouped
    read, so a request needing node and commitment figures prices the ledger once. A row with
    no API price (a local model: ``not_api_priced``) adds no dollars and is not unknown; a row
    Measurement cannot price (no list price, refused by admission, an unproven context tier,
    an unpublished cache rate) counts in ``unknown_rows``. ``known`` is None when none of the
    id's rows has a price.
    """
    dollars: dict[str, dict[str, dict]] = {column: {} for column in ids}
    wanted = {column: sorted({str(value) for value in values}) for column, values in ids.items() if values}
    if not wanted:
        return dollars
    # Each id list is one JSON parameter, so no list is chunked and no row is read twice.
    rows = [dict(row) for row in fetch_all(connection, grouped_usage_sql(
        {column: f"u.{column}" for column in wanted},
        "llm_usage u WHERE " + " OR ".join(
            f"u.{column} IN (SELECT value FROM json_each(?))" for column in wanted
        ),
    ), tuple(json.dumps(values) for values in wanted.values()))]
    if not rows:
        return dollars
    resolve = IdentityResolver(connection)
    for row in rows:
        row["model"] = resolve(row).key
    members = {column: set(values) for column, values in wanted.items()}
    for row in classify_usage_rows(rows, endpoint_listings(connection)):
        for column, listed in members.items():
            if str(row[column]) not in listed:
                continue
            amount = dollars[column].setdefault(str(row[column]), {"known": None, "unknown_rows": 0})
            if row["outcome"] == "priced":
                amount["known"] = (amount["known"] or 0) + row["dollars"]
            elif row["outcome"] != "not_api_priced":
                amount["unknown_rows"] += int(row["records"])
    return dollars


def own_usage(
    connection, node_ids: list[str], dollars: Mapping[str, dict] | None, rows_in: Callable,
) -> dict[str, dict]:
    """Each node's own tokens and ``model_cost_usd``; ``dollars`` None prices them here."""
    rows = rows_in(
        connection,
        f"""SELECT plan_node_id,{USAGE_TOKENS} FROM llm_usage
            WHERE plan_node_id IN ({{marks}}) GROUP BY plan_node_id""",
        node_ids,
    )
    if dollars is None:
        dollars = usage_dollars(
            connection, {"plan_node_id": [str(row["plan_node_id"]) for row in rows]},
        )["plan_node_id"]
    return {
        str(row["plan_node_id"]): {
            "tokens": {"known": row["tokens"], "unknown_rows": row["unknown_tokens"]},
            "model_cost_usd": dollars.get(str(row["plan_node_id"]), {"known": None, "unknown_rows": 0}),
        }
        for row in rows
    }
