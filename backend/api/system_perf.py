"""Local performance-report API used by the CORSIA CLI."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, NonNegativeFloat, NonNegativeInt, PositiveInt

from infra.auth import require_user
from infra.client_request_timing import (
    BATCH_ROWS, BUCKET_COUNT, MAX_ISSUE_KEYS, MAX_WINDOW_DAYS, ROUTE_MAX_LENGTH,
)

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

    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    engine: Literal["chromium", "webkit", "gecko", "other"]
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "OTHER"]
    route: str = Field(max_length=ROUTE_MAX_LENGTH)
    measure: Literal["total", "wait", "connect", "server", "network"]
    count: PositiveInt
    sum_ms: NonNegativeFloat
    max_ms: NonNegativeFloat
    buckets: list[NonNegativeInt] = Field(min_length=BUCKET_COUNT, max_length=BUCKET_COUNT)


class ClientTimingBatch(BaseModel):
    """A page's rows, and counts of what went wrong in its own capture."""

    engine: Literal["chromium", "webkit", "gecko", "other"]
    rows: list[ClientTimingRow] = Field(default_factory=list, max_length=BATCH_ROWS)
    issues: dict[str, int] = Field(default_factory=dict, max_length=MAX_ISSUE_KEYS)


@router.post("/client-timing", status_code=202)
def post_client_timing(
    batch: ClientTimingBatch, request: Request, user: dict = Depends(require_user),
):
    """Store the browser's request timing aggregates. A row whose route is not a registered template is dropped and counted."""
    from infra.client_request_timing import ingest, registered_route_templates

    kept, dropped = ingest(
        batch.engine,
        [(r.day, r.engine, r.method, r.route, r.measure, r.count, r.sum_ms, r.max_ms, r.buckets) for r in batch.rows],
        batch.issues,
        registered_route_templates(request.app),
    )
    return {"kept": kept, "dropped": dropped}


@router.get("/client-timing")
def client_timing(
    days: Annotated[int, Query(ge=1, le=MAX_WINDOW_DAYS)] = 7,
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
