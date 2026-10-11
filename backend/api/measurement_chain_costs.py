"""The accepted tasks' classified cost rows and recording gaps over the grain's two windows, read once per page load.

Models reads every task's rows and recording gaps; Agents' dollars per hour read the cost rows of that same result.
A task's rows are the same whichever tasks were walked with it.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from api.cost_identity import IdentityResolver
from api.measurement_join import joined_read
from api.measurement_model_cost import materialize_chain, query_model_costs, query_recording_gaps
from api.measurement_scope import validate_scope
from core.measurement_scope import Grain
from infra.database import get_db
from infra.db_async import run_bulk


def two_windows(grain: Grain) -> Grain:
    """The same grain narrowed to the periods of its latest and previous windows.

    The windows are the grain's own and do not move; only the periods nothing here compares are
    dropped, so a task's accepted period and its chain are read for those two windows alone.
    """
    keep = len(grain.latest.periods) + len(grain.previous.periods)
    return replace(grain, periods=grain.periods[-keep:], starts=grain.starts[-(keep + 1):])


def window_params(scope_params: dict[str, Any], grain: Grain) -> dict[str, Any]:
    """The scope's params over the grain's two windows: the periods Models and Agents' $/hour both read."""
    return {**scope_params, "grain": two_windows(grain)}


def chain_costs(db, scope_params: dict[str, Any], grain: Grain) -> dict[str, Any]:
    """Every accepted task's classified usage and recording gaps over the two windows.

    The one shared read walks each task's chain once. Agents consumes only ``cost_rows``; Models also consumes
    ``gaps``.
    """
    params = window_params(scope_params, grain)
    chain = materialize_chain(db, params)
    try:
        cost_rows = query_model_costs(db, params, IdentityResolver(db), chain_rows=chain)
        gaps = query_recording_gaps(db, params, chain_rows=chain)
    finally:
        db.execute(f"DROP TABLE IF EXISTS {chain}")  # the pooled connection outlives the request
    return {"cost_rows": cost_rows, "gaps": sorted(gaps)}


@joined_read
async def task_chain_costs(project: str, agent: str, page_grain: Grain, user: dict) -> dict[str, Any]:
    """:func:`chain_costs` for one page load, shared by the Models and Agents reads that overlap in flight.

    Its own bulk job, so a reader waits for it outside the lane: no reader holds a worker while it waits.
    """

    def read() -> dict[str, Any]:
        db = get_db()
        try:
            return chain_costs(db, validate_scope(db, user["id"], project, agent).params(user["id"]), page_grain)
        finally:
            db.close()

    return await run_bulk(read)
