"""The usage ledger's tokens and dollars for plan nodes and commitments.

Tokens are summed from ``llm_usage``. Dollars are Measurement's API-equivalent price of
the same rows, through ``core.measurement_pricing``: the grouped read, admission and
``classify_usage_rows`` that Measurement's Cost and Models read. A row's stored
``cost_usd`` is not read.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping
from typing import NamedTuple

from core.cost_identity import IdentityResolver
from core import node_usage_groups
from core.measurement_pricing import classify_usage_rows, endpoint_listings, grouped_stored_sql, grouped_usage_sql
from infra.node_usage_schema import NODE_GROUPS_TABLE
from infra.packed_rows import fetch_all
from infra.turn_usage_remainder import pending_measurement_counts

#: Tokens summed in SQL, so a read returns one row per group however many calls
#: the ledger holds. ``SUM`` is NULL when every value is: "no known amount".
USAGE_TOKENS = "SUM(total_tokens) AS tokens,SUM(total_tokens IS NULL) AS unknown_tokens"
#: ``USAGE_TOKENS`` over stored node groups. A group holding a total that is neither NULL nor an
#: integer (``total_odd``) is never read this way: its key is read from the ledger instead.
STORED_TOKENS = ("CASE WHEN SUM(total_known)>0 THEN SUM(total_tokens) END AS tokens,"
                 "SUM(records-total_known-total_odd) AS unknown_tokens")
#: Ledger columns the stored groups are keyed by.
_STORED_COLUMNS = frozenset({"plan_node_id", "plan_commitment_id"})


def stored_tokens(connection, column: str, ids: list[str], extra: tuple[str, ...], ledger_sql: str, rows_in: Callable):
    """``USAGE_TOKENS`` grouped by ``column`` (plus ``extra`` columns) for ``ids``, read from the stored groups.

    ``ledger_sql`` is the caller's own ledger read, with one ``{marks}`` placeholder; it runs instead when
    the stored groups do not yet cover every node, and for any id whose groups hold an odd total. Rows come
    back in the ledger read's order: by ``column`` then ``extra``.
    """
    if not ids or not node_usage_groups.ready(connection):
        return rows_in(connection, ledger_sql, ids)
    group = ",".join((column, *extra))
    rows = rows_in(connection, f"""SELECT {group},{STORED_TOKENS},SUM(total_odd) AS odd FROM {NODE_GROUPS_TABLE}
        WHERE {column} IN ({{marks}}) GROUP BY {group}""", ids)
    odd = sorted({str(row[column]) for row in rows if row["odd"]})
    kept = [{key: row[key] for key in (column, *extra, "tokens", "unknown_tokens")} for row in rows
            if str(row[column]) not in odd]
    if not odd:
        return kept
    merged = kept + [dict(row) for row in rows_in(connection, ledger_sql, odd)]
    return sorted(merged, key=lambda row: tuple((row[key] is not None, row[key] or "") for key in (column, *extra)))


def usage_dollars(connection, ids: Mapping[str, Collection[str]], *, include_turns: bool = False) -> dict[str, dict[str, dict]]:
    """Each id's ``model_cost_usd``: Measurement's API-equivalent price of its ``llm_usage`` rows.

    ``ids`` maps a ledger column (``plan_node_id``, ``plan_commitment_id``) to the ids it names;
    the result maps each column to ``{id: amount}`` for every id named, one with no rows
    included. All columns are priced in one grouped read, so a request needing node and
    commitment figures prices the ledger once. A row with
    no API price (a local model: ``not_api_priced``) adds no dollars and is not unknown; a row
    Measurement cannot price (no list price, refused by admission, an unproven context tier,
    an unpublished cache rate) counts in ``unknown_rows``. ``known`` is None when none of the
    id's rows has a price; ``records`` counts all of its rows, so None with ``records`` above 0
    and nothing unknown is a priced-nothing id (all local), and ``records`` 0 is an id with no usage.
    When ``include_turns`` is true each amount also includes the set of measured turn IDs, gathered
    in the same read; this increases grouping granularity and is used only by thread cost evaluation.
    """
    wanted = {column: sorted({str(value) for value in values}) for column, values in ids.items() if values}
    dollars: dict[str, dict[str, dict]] = {
        column: {value: {"known": None, "unknown_rows": 0, "records": 0} for value in wanted.get(column, ())} for column in ids
    }
    if not wanted:
        return dollars
    if include_turns:
        for amounts in dollars.values():
            for amount in amounts.values():
                amount["turns"] = set()
    # Each id list is one JSON parameter, so no list is chunked and no row is read twice.
    # Node and commitment ids read the stored groups (``core.node_usage_groups``) once every node
    # is rebuilt under the current definition: the same grouped rows, from far fewer stored ones.
    stored = set(wanted) <= _STORED_COLUMNS and node_usage_groups.ready(connection)
    alias = "g" if stored else "u"
    keys = {column: f"{alias}.{column}" for column in wanted}
    if include_turns:
        keys["turn_id"] = f"{alias}.turn_id"
    picks = " OR ".join(f"{alias}.{column} IN (SELECT value FROM json_each(?))" for column in wanted)
    sql = (grouped_stored_sql(keys, f"{NODE_GROUPS_TABLE} g WHERE {picks}") if stored
           else grouped_usage_sql(keys, f"llm_usage u WHERE {picks}"))
    rows = [dict(row) for row in fetch_all(connection, sql, tuple(json.dumps(values) for values in wanted.values()))]
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
            amount = dollars[column][str(row[column])]
            amount["records"] += int(row["records"])
            if include_turns and row["turn_id"] is not None:
                amount["turns"].add(row["turn_id"])
            if row["outcome"] == "priced":
                amount["known"] = (amount["known"] or 0) + row["dollars"]
            elif row["outcome"] != "not_api_priced":
                amount["unknown_rows"] += int(row["records"])
    return dollars


class Ledger(NamedTuple):
    """Commitments' ``usage_dollars`` and the receipts still pending for them, read before it."""

    dollars: Mapping[str, dict]
    pending: Mapping[str, dict[str, int]]


def priced_with_pending(
    connection, node_ids: Collection[str], commitment_ids: Collection[str],
) -> tuple[dict[str, dict], Ledger]:
    """The nodes' ``usage_dollars`` and the commitments' ``Ledger``, priced in one read.

    The commitments' pending receipts are read first: the projection writing a call's row
    deletes its receipt in the same transaction, so a call committing between the two reads
    is briefly counted unknown by a row that covers it, never missed by both and reported
    short (``commitment_cost``).
    """
    commitments = sorted({str(value) for value in commitment_ids})
    pending = pending_measurement_counts(connection, commitments) if commitments else {}
    dollars = usage_dollars(connection, {"plan_node_id": node_ids, "plan_commitment_id": commitments})
    return dollars["plan_node_id"], Ledger(dollars["plan_commitment_id"], pending)


def amount(dollars: Mapping[str, dict], item_id: str) -> dict:
    """``item_id``'s amount in a ``usage_dollars`` answer, which names every id it priced.

    An id it never priced is a caller's defect, not "no API price": it fails here rather
    than read as no dollars and nothing unknown.
    """
    try:
        return dollars[item_id]
    except KeyError:
        raise LookupError(f"{item_id} is not in the priced read passed in; price every projected id") from None


def own_usage(
    connection, node_ids: list[str], dollars: Mapping[str, dict] | None, rows_in: Callable,
) -> dict[str, dict]:
    """Each node's own tokens and ``model_cost_usd``; ``dollars`` None prices them here.

    A ``dollars`` map passed in must cover every node with usage (``amount``).
    """
    rows = stored_tokens(
        connection, "plan_node_id", node_ids, (),
        f"""SELECT plan_node_id,{USAGE_TOKENS} FROM llm_usage
            WHERE plan_node_id IN ({{marks}}) GROUP BY plan_node_id""",
        rows_in,
    )
    if dollars is None:
        dollars = usage_dollars(connection, {"plan_node_id": node_ids})["plan_node_id"]
    # The node figure keeps its two published fields; ``records`` is for ``usage_dollars``' other readers.
    priced = {str(row["plan_node_id"]): amount(dollars, str(row["plan_node_id"])) for row in rows}
    return {
        str(row["plan_node_id"]): {
            "tokens": {"known": row["tokens"], "unknown_rows": row["unknown_tokens"]},
            "model_cost_usd": {key: priced[str(row["plan_node_id"])][key] for key in ("known", "unknown_rows")},
        }
        for row in rows
    }
