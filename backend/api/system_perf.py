"""Local performance-report API used by the CORSIA CLI."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from infra.auth import require_user
from infra.client_request_timing import MAX_ROWS_PER_BATCH

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


class ClientTimingRow(BaseModel):
    """One aggregate the page measured: a day, engine, method, route template and measure."""

    day: str = Field(max_length=10)
    engine: str = Field(max_length=16)
    method: str = Field(max_length=16)
    route: str = Field(max_length=256)
    measure: str = Field(max_length=16)
    count: int = Field(ge=1)
    sum_ms: float = Field(ge=0)
    max_ms: float = Field(ge=0)
    buckets: list[int] = Field(max_length=32)


class ClientTimingBatch(BaseModel):
    rows: list[ClientTimingRow] = Field(max_length=MAX_ROWS_PER_BATCH)


@router.post("/client-timing", status_code=202)
def post_client_timing(batch: ClientTimingBatch, user: dict = Depends(require_user)):
    """Store the browser's request timing aggregates; rows outside what the page can send are dropped."""
    from infra.client_request_timing import ingest

    kept = ingest([(r.day, r.engine, r.method, r.route, r.measure, r.count, r.sum_ms, r.max_ms, r.buckets)
                   for r in batch.rows])
    return {"kept": kept}


@router.get("/client-timing")
def client_timing(
    days: int = Query(7, ge=1, le=90),
    user: dict = Depends(require_user),
):
    """Return browser-side timing aggregates: rows of day, engine, method, route, measure, count, sum, max, buckets."""
    from infra.analytics_db import get_db
    from infra.client_request_timing import query_client_timing

    cur = get_db().cursor()
    try:
        return {"rows": [[str(row[0]), *row[1:]] for row in query_client_timing(cur, days)]}
    finally:
        cur.close()
