"""Presence-only attention at the grain of the agent that owns each thread."""

from __future__ import annotations

from typing import Any

from api.measurement_prepared import PREPARED_SCOPE
from api.measurement_presence import presence_allocation_cte
from api.measurement_presence_rollup import rolled_root_cte
from api.measurement_scope import query_params
from api.presence_rollup import rollup_binds
from core.measurement_scope import Grain


def root_active_family_sql(grain: Grain, scope_threads: str, *, rolled: bool = False) -> str:
    """Return equally allocated presence time for each agent-period.

    The shared interval allocator divides every overlapping threaded slice evenly,
    so root rows add to the threaded All-work total without double counting. ``rolled``
    adds each root's share of the rollup time before ``:focus_rollup_through``.
    """
    rollup = f", {rolled_root_cte(grain, scope_threads)}" if rolled else ""
    rolled_rows = "\n          UNION ALL SELECT root_id,began,seconds FROM presence_rollup_roots" if rolled else ""
    return f"""
      WITH {presence_allocation_cte(grain, rolled=rolled)}{rollup}, root_slices AS (
        SELECT s.root_id,p.began,p.seconds FROM presence_thread_slices p JOIN {scope_threads} s ON s.id=p.thread_id{rolled_rows}
      )
      SELECT root_id,{grain.bucket('began')} AS period,SUM(seconds)/3600.0 AS active_hours
        FROM root_slices
        WHERE {grain.bucket('began')} IS NOT NULL GROUP BY root_id,period ORDER BY root_id,period
    """


def query_root_active(db, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ``{root_id, period, active_hours}`` for each agent-period in the request."""
    bound = query_params(params)
    bound.update(rollup_binds(db, bound))
    sql = root_active_family_sql(bound["grain"], PREPARED_SCOPE, rolled="focus_rollup_through" in bound)
    return [dict(row) for row in db.execute(sql, bound).fetchall()]
