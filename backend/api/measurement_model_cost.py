"""Accepted-task usage and API-equivalent prices per model identity for Models.

Task grain: a model's tasks in a period are the accepted tasks settled that period
whose chain (``measurement_chain``: the task's attempts, everything delegated
under them, and sent-back predecessors) has usage on that model through first
acceptance. Its rate is the API-equivalent price of its own calls in those
chains divided by those tasks. Pricing is Cost's own
admission and classification (``ADMISSION_SQL`` / ``classify_usage_rows``)
applied to the row's resolved identity, so a call Cost cannot price stays
unknown here rather than counting as free. A task whose call failed to reach
the ledger at all is reported by ``query_recording_gaps``, the node's own
``usage_recording_failed`` events that per-node Cost reads, so its figure is
marked possibly low rather than presented as complete.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping
from typing import Any

from api.cost_identity import IDENTITY_GROUP, ModelIdentity, identity_columns, identity_join
from api.measurement_chain import chain_ctes
from api.measurement_cost import ADMISSION_SQL, IDLE_HOURS_SQL, INPUT_TIER_SQL, classify_usage_rows, endpoint_listings
from api.measurement_prepared import CHAIN_DDL, CHAIN_TABLE
from api.measurement_queries import outcome_ctes
from api.measurement_scope import query_params, scope_cte

_BUCKETS = ("prompt_tokens", "completion_tokens", "cache_creation_tokens", "cache_read_tokens")
_ADMITTED = ",".join(f"SUM(CASE WHEN admission='ok' THEN {bucket} ELSE 0 END) AS {bucket}" for bucket in _BUCKETS)


def _bound(params: dict[str, Any], task_ids: Collection[str] | None) -> dict[str, Any]:
    """The query binds, with the task ids a caller narrowed the read to."""
    bound = query_params(params)
    return bound if task_ids is None else {**bound, "task_ids": json.dumps(sorted(task_ids))}


# The accepted tasks and the nodes of their chains: ``task_id, period, settled_at, node_id``.
_CHAIN_ROWS = "(SELECT o.id AS task_id,o.period,o.settled_at,c.node_id FROM outcome_rows o JOIN chain c ON c.task_id=o.id)"


def _cost_sql(chain_rows: str) -> str:
    """The grouped usage read over ``chain_rows``: calls are grouped on what classifies them first,
    and the context manifest is joined afterwards, onto those few groups instead of onto every call."""
    sums = ",".join(f"SUM(u.{bucket}) AS {bucket}" for bucket in _BUCKETS)
    return f"""
        SELECT period,task_id,{IDENTITY_GROUP},admission,input_tier,idle_hours,SUM(records) AS records,{_ADMITTED}
        FROM (
          SELECT g.period AS period,g.task_id AS task_id,{identity_columns("g")},
            g.admission AS admission,g.input_tier AS input_tier,g.idle_hours AS idle_hours,g.records AS records,
            g.prompt_tokens AS prompt_tokens,g.completion_tokens AS completion_tokens,
            g.cache_creation_tokens AS cache_creation_tokens,g.cache_read_tokens AS cache_read_tokens
          FROM (
            SELECT x.period AS period,x.task_id AS task_id,u.model AS model,u.context_manifest_id AS context_manifest_id,
              {ADMISSION_SQL} AS admission,{INPUT_TIER_SQL} AS input_tier,{IDLE_HOURS_SQL} AS idle_hours,
              COUNT(*) AS records,{sums}
            FROM {chain_rows} x JOIN llm_usage u ON u.plan_node_id=x.node_id AND u.created_at<=x.settled_at
            GROUP BY x.period,x.task_id,u.model,u.context_manifest_id,admission,input_tier,idle_hours
          ) g {identity_join("g")}
        )
        GROUP BY period,task_id,{IDENTITY_GROUP},admission,input_tier,idle_hours
    """


def materialize_chain(db, params: dict[str, Any], task_ids: Collection[str] | None = None) -> str:
    """Walk the chains of the prepared periods' accepted tasks (or only ``task_ids``) once into a temporary table
    the readers below can share.

    Returns the table to pass as ``chain_rows``; it belongs to the request connection and the next call replaces its
    rows, so the caller empties it when done.

    The table is created once per connection, before any write (see ``create_tables``), and refilled, never dropped:
    creating or dropping a table changes the connection's schema, which expires every statement it has prepared, so
    each would be planned again on its next run, whichever job sends it."""
    prefix, source, bound = _chain_source(params, task_ids, None)
    db.execute(CHAIN_DDL)
    db.execute(f"DELETE FROM {CHAIN_TABLE}")
    db.execute(f"INSERT INTO {CHAIN_TABLE} {prefix} SELECT task_id,period,settled_at,node_id FROM {source}", bound)
    return CHAIN_TABLE


def _chain_source(params: dict[str, Any], task_ids: Collection[str] | None, chain_rows: str | None) -> tuple[str, str, dict[str, Any]]:
    """The CTE prefix, the chain rows to select from and the binds: the walk itself, unless a table already holds it."""
    if chain_rows is not None:
        return "", chain_rows, {}
    prefix = scope_cte() + outcome_ctes(query_params(params)["grain"], limited_to_range=False) + chain_ctes(only_tasks=task_ids is not None)
    return prefix, _CHAIN_ROWS, _bound(params, task_ids)


def query_model_costs(
    db, params: dict[str, Any], resolve: Callable[[Mapping[str, Any]], ModelIdentity],
    task_ids: Collection[str] | None = None, chain_rows: str | None = None,
) -> list[dict[str, Any]]:
    """Return each accepted task's classified usage per identity, for the periods the page grain prepared.

    ``task_ids`` narrows the read to those tasks' chains; their figures are unchanged. ``chain_rows`` is the table
    :func:`materialize_chain` filled for them, so a caller that also reads the gaps walks the chains once."""
    prefix, source, bound = _chain_source(params, task_ids, chain_rows)
    rows = []
    for row in (dict(item) for item in db.execute(prefix + _cost_sql(source), bound).fetchall()):
        identity = resolve(row)
        rows.append({**row, "model": identity.key, "identity": identity})
    return classify_usage_rows(rows, endpoint_listings(db))


def query_recording_gaps(db, params: dict[str, Any], task_ids: Collection[str] | None = None,
                         chain_rows: str | None = None) -> set[str]:
    """Accepted tasks in the prepared periods with a call that failed to record before acceptance."""
    prefix, source, bound = _chain_source(params, task_ids, chain_rows)
    sql = prefix + f"""
        SELECT DISTINCT x.task_id AS task_id FROM {source} x
        JOIN plan_node_events e ON e.node_id=x.node_id AND e.event='usage_recording_failed'
          AND e.created_at<=x.settled_at
    """
    return {str(row["task_id"]) for row in db.execute(sql, bound).fetchall()}
