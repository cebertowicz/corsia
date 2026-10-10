"""The period still in progress: accepted outcomes so far, read apart from the completed-period spine."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from api.measurement_queries import outcome_ctes
from core.measurement_scope import Grain, scope_cte


def stored_bound(instant: datetime) -> str:
    """Format a UTC bound the way the store writes timestamps, so text comparison is exact."""
    return instant.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


def grain_instant(text: str) -> datetime:
    """A grain boundary (``YYYY-MM-DD HH[:MM]``, UTC) as an instant."""
    return datetime.strptime(text, "%Y-%m-%d %H" if len(text) == 13 else "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def current_period_outcomes(db, scope_params: dict[str, Any], grain: Grain) -> int:
    """Accepted outcomes in the scope since the period in progress began, by the same settlement rule as the series."""
    params = {**scope_params, "from_ts": stored_bound(grain_instant(grain.starts[-1])),
              "to_ts": stored_bound(grain_instant(grain.current_end))}
    return int(db.execute(scope_cte() + outcome_ctes(limited_to_range=False) + """
        SELECT COUNT(*) AS outcomes FROM outcome_rows WHERE settled_at >= :from_ts AND settled_at < :to_ts
    """, params).fetchone()["outcomes"])
