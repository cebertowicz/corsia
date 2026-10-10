"""Human attention time at the grain of the agent that owns each thread.

The Agents table's minutes per outcome divides this by the agent's outcomes. It is
the scope-wide active family's rule (a human message is a minute, and a gap under
the cap counts), applied to each agent's own human messages.
"""

from __future__ import annotations

from typing import Any

from api.measurement_queries import PREPARED_SCOPE, range_filter
from api.measurement_scope import query_params
from core.measurement_scope import Grain

#: The longest silence between two human messages that still counts as attention.
MAX_ACTIVE_GAP_SECONDS = 600


def root_active_family_sql(grain: Grain, scope_threads: str) -> str:
    """Return the active family at the grain of the agent that owns each thread.

    The same rule as ``active_family_sql`` (a human message is a minute, and a gap
    under the cap counts), applied to each agent's own human messages: the time a
    person spent on that agent's threads, gaps measured inside it.
    """
    return f"""
      WITH pre_candidates AS (
        SELECT s.root_id AS root_id,m.id AS id,m.created_at AS created_at,
          ROW_NUMBER() OVER (PARTITION BY s.root_id ORDER BY m.created_at DESC,m.id DESC) AS rn
        FROM {scope_threads} s JOIN messages m ON m.id=(
          SELECT prior.id FROM messages prior WHERE prior.thread_id=s.id
            AND prior.role='user' AND COALESCE(json_extract(prior.metadata,'$.sender_kind'),'')='' AND prior.created_at < :from_ts
          ORDER BY prior.created_at DESC,prior.id DESC LIMIT 1
        )
      ), human_messages AS (
        SELECT root_id,id,created_at,LAG(created_at) OVER (PARTITION BY root_id ORDER BY created_at,id) AS prior_at FROM (
          SELECT s.root_id AS root_id,m.id AS id,m.created_at AS created_at FROM messages m
          JOIN {scope_threads} s ON s.id=m.thread_id
          WHERE m.role='user' AND COALESCE(json_extract(m.metadata,'$.sender_kind'),'')='' AND {range_filter("m.created_at")}
          UNION ALL SELECT root_id,id,created_at FROM pre_candidates WHERE rn=1
        )
      ) SELECT root_id,{grain.bucket('created_at')} AS period,
          SUM(60 + CASE WHEN prior_at IS NULL THEN 0 WHEN strftime('%s',created_at)-strftime('%s',prior_at)<=:max_active_gap_seconds
          THEN MAX(0,strftime('%s',created_at)-strftime('%s',prior_at)) ELSE 0 END)/3600.0 AS active_hours
        FROM human_messages WHERE {grain.bucket('created_at')} IS NOT NULL
        GROUP BY root_id,period ORDER BY root_id,period
    """


def query_root_active(db, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ``{root_id, period, active_hours}`` for each agent-period in the request."""
    bound = {"max_active_gap_seconds": MAX_ACTIVE_GAP_SECONDS, **query_params(params)}
    return [dict(row) for row in db.execute(root_active_family_sql(bound["grain"], PREPARED_SCOPE), bound).fetchall()]
