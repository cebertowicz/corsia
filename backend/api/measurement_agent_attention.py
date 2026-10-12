"""Presence-only attention at the grain of the agent that owns each thread."""

from __future__ import annotations

from typing import Any

from api.measurement_prepared import PREPARED_SCOPE
from api.measurement_presence import presence_allocation_cte
from api.measurement_scope import query_params
from core.measurement_scope import Grain


def root_active_family_sql(grain: Grain, scope_threads: str) -> str:
    """Return equally allocated presence time for each agent-period.

    The shared interval allocator divides every overlapping threaded slice evenly,
    so root rows add to the threaded All-work total without double counting.
    """
    return f"""
      WITH {presence_allocation_cte(grain)}
      SELECT s.root_id AS root_id,{grain.bucket('p.began')} AS period,SUM(p.seconds)/3600.0 AS active_hours
        FROM presence_thread_slices p JOIN {scope_threads} s ON s.id=p.thread_id
        WHERE {grain.bucket('p.began')} IS NOT NULL GROUP BY s.root_id,period ORDER BY s.root_id,period
    """


def query_root_active(db, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ``{root_id, period, active_hours}`` for each agent-period in the request."""
    bound = query_params(params)
    return [dict(row) for row in db.execute(root_active_family_sql(bound["grain"], PREPARED_SCOPE), bound).fetchall()]
