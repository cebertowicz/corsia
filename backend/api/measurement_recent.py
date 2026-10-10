"""Measurement key figures: the grain's latest window of completed periods beside the one before."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query

from api.measurement_kpi import Period, build_kpi
from api.measurement_partial import grain_instant, stored_bound
from api.measurement_prepared import DDL, PREPARED_SCOPE
from api.measurement_scope import resolve_grain, scope_cte, validate_scope
from infra.auth import require_user
from infra.database import get_db
from infra.db_async import run_bulk

router = APIRouter()


@router.get("/key-figures")
async def measurement_key_figures(
    project: str = Query(...), agent: str = Query(...), timezone_name: str = Query(..., alias="timezone"),
    grain: str = "days", user: dict = Depends(require_user),
):
    """Return the last 7 completed days (or 4 completed periods) beside the same number before."""
    page_grain = resolve_grain(grain, timezone_name)
    request_now = datetime.now(timezone.utc)
    windows = {"current": page_grain.latest, "previous": page_grain.previous}
    periods = {name: Period(*(stored_bound(grain_instant(text)) for text in page_grain.instants(window)))
               for name, window in windows.items()}
    current, previous, today = periods["current"], periods["previous"], page_grain.current

    def query() -> tuple[dict[str, str | None], dict[str, Any]]:
        """Read the scope and the two windows' token figures on one connection."""
        db = get_db()
        try:
            selected_scope = validate_scope(db, user["id"], project, agent)
            params = {**selected_scope.params(user["id"]), "previous_from": previous.start, "current_to": current.end,
                      "from_ts": previous.start, "to_ts": current.end}
            # The scope closure under its canonical name for the usage read.
            db.execute(DDL[PREPARED_SCOPE])
            db.execute(f"DELETE FROM {PREPARED_SCOPE}")
            db.execute(scope_cte() + f"INSERT INTO {PREPARED_SCOPE}(id,root_id) SELECT id,root_id FROM scope_threads", params)
            kpi = build_kpi(db, current, previous)
            for name, window in windows.items():
                kpi[name].update({"from": window.first, "to": page_grain.period_end(window.last)})
            return selected_scope.response(), kpi
        finally:
            db.close()

    selected_scope, kpi = await run_bulk(query)
    return {"definition_version": 1, "scope": selected_scope, "timezone": timezone_name,
            "generated_at": request_now.isoformat(), "today": today, "period": page_grain.describe(), "kpi": kpi}
