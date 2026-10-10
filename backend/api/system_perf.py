"""Local performance-report API used by the CORSIA CLI."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from infra.auth import require_user

router = APIRouter(prefix="/api/system/perf", tags=["system"])


@router.get("/routes")
def route_timing(
    days: int = Query(7, ge=1, le=90),
    user: dict = Depends(require_user),
):
    """Return historical aggregate route timing without request content."""
    from infra.analytics_db import get_db
    from infra.request_timing import query_route_timing

    cur = get_db().cursor()
    try:
        return {"rows": [list(row) for row in query_route_timing(cur, days)]}
    finally:
        cur.close()
