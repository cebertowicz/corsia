"""Latest-window turn reads for the Models comparison."""

from __future__ import annotations

from typing import Any

from core.cost_identity import IDENTITY_GROUP, identity_columns, identity_join
from api.measurement_queries import PREPARED_SCOPE, query_params


def query_model_turns(db, params: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return the grain's latest window's turn totals and error texts per identity shape.

    ``params["grain"]`` names the window: a turn counts when it ended inside it, so a day
    or week that crosses a DST change keeps its own length.
    """
    grain = params["grain"]
    bound = query_params(params)
    where = (
        f"t.thread_id IN (SELECT id FROM {PREPARED_SCOPE}) "
        f"AND {grain.window_sql('t.ended_at', grain.latest)}"
    )
    totals = [dict(row) for row in db.execute(f"""
      SELECT {identity_columns("t")},COUNT(*) AS turns FROM active_turns t {identity_join("t")}
      WHERE {where} GROUP BY {IDENTITY_GROUP}
    """, bound).fetchall()]
    errors = [dict(row) for row in db.execute(f"""
      SELECT {identity_columns("t")},t.error AS error FROM active_turns t {identity_join("t")}
      WHERE {where} AND t.status='error'
    """, bound).fetchall()]
    return totals, errors
