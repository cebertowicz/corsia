"""Human attention time at the grain of the agent that owns each thread.

The Agents table's minutes per outcome divides this by the agent's outcomes. Each
thread's minutes are the ones the scope-wide active family counts (a human message is
a minute, and a gap under the cap counts, measured across the person's whole stream),
so the agents' minutes add up to the All-work figure.
"""

from __future__ import annotations

from typing import Any

from api.measurement_queries import PREPARED_SCOPE, human_minutes_cte
from api.measurement_scope import query_params
from core.measurement_scope import Grain

#: The longest silence between two human messages that still counts as attention.
MAX_ACTIVE_GAP_SECONDS = 600


def root_active_family_sql(grain: Grain, scope_threads: str) -> str:
    """Return the active family at the grain of the agent that owns each thread.

    Each thread's minutes are the rows ``active_family_sql`` sums (a human message is
    a minute, and a gap under the cap counts), grouped by the agent that owns the
    thread. The gap is measured across the person's whole stream, so an agent's row
    equals its own page and the rows add up to the All-work figure.
    """
    return f"""
      WITH {human_minutes_cte(grain)}
      SELECT s.root_id AS root_id,h.period AS period,SUM(h.seconds)/3600.0 AS active_hours
        FROM human_minutes h JOIN {scope_threads} s ON s.id=h.thread_id
        WHERE h.period IS NOT NULL GROUP BY s.root_id,h.period ORDER BY s.root_id,h.period
    """


def query_root_active(db, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ``{root_id, period, active_hours}`` for each agent-period in the request."""
    bound = {"max_active_gap_seconds": MAX_ACTIVE_GAP_SECONDS, **query_params(params)}
    return [dict(row) for row in db.execute(root_active_family_sql(bound["grain"], PREPARED_SCOPE), bound).fetchall()]
