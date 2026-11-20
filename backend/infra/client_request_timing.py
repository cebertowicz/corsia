"""Browser-side request timing: how long each /api request waited in the browser versus ran on the server.

The page reads each /api request's resource timing entry, aggregates it per day, engine, method and
route template, and posts small batches here. Rows are aggregate-only (counts, sums, a maximum and a
histogram), never a path, an id or a person. A measure the browser did not fill for a request is not
recorded for it, so its row count can be lower than ``total`` and unknown is never stored as zero.

Measures, all in milliseconds:

- ``total``: start of the fetch until the last response byte.
- ``wait``: start of the fetch until the request was sent, minus connection setup. Queueing behind the
  browser's per-host connection limit and other stalls land here.
- ``connect``: name lookup, connect and TLS for a request that opened a connection.
- ``server``: the server's own duration, from its Server-Timing ``app`` entry.
- ``network``: request sent until last byte, minus the server's duration: transfer and travel.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from infra.product_events import analytics_disabled
from infra.request_timing import _BUCKET_UPPERS

log = logging.getLogger(__name__)

MEASURES = ("total", "wait", "connect", "server", "network")
ENGINES = ("chromium", "webkit", "gecko", "other")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "OTHER")
BUCKET_COUNT = len(_BUCKET_UPPERS)
MAX_ROWS_PER_BATCH = 500
MAX_ROUTE_LENGTH = 160
#: A batch may carry a day the client's clock put on either side of the server's UTC day.
_DAY_SLACK = timedelta(days=2)

Row = tuple[date, str, str, str, str, int, float, float, list[int]]


Item = tuple[str, str, str, str, str, int, float, float, list[int]]


def validate_row(item: Item, today: date) -> Row | None:
    """Return one storable row, or None when any field is outside what the page can send."""
    day, engine, method, route, measure, count, sum_ms, max_ms, buckets = item
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        return None
    if abs(parsed - today) > _DAY_SLACK:
        return None
    if (engine not in ENGINES or method not in METHODS or measure not in MEASURES
            or not route.startswith("/") or len(route) > MAX_ROUTE_LENGTH):
        return None
    if (count < 1 or sum_ms < 0 or max_ms < 0 or len(buckets) != BUCKET_COUNT
            or any(b < 0 for b in buckets) or sum(buckets) != count):
        return None
    return (parsed, engine, method, route, measure, count, sum_ms, max_ms, buckets)


def write_rows(conn, rows: list[Row]) -> None:
    """Merge rows into the daily table in one DuckDB transaction; raise on failure."""
    if not rows:
        return
    cur = conn.cursor()
    try:
        cur.sql("BEGIN")
        values = ", ".join("(?, ?, ?, ?, ?, ?, ?, ?, ?)" for _ in rows)
        params = [value for row in rows for value in row]
        cur.execute(
            "INSERT INTO client_request_timing_daily (day, engine, method, route, measure, count, sum_ms, "
            "max_ms, buckets) VALUES " + values + " "
            "ON CONFLICT (day, engine, method, route, measure) DO UPDATE SET "
            "count = client_request_timing_daily.count + excluded.count, "
            "sum_ms = client_request_timing_daily.sum_ms + excluded.sum_ms, "
            "max_ms = GREATEST(client_request_timing_daily.max_ms, excluded.max_ms), "
            "buckets = list_transform(list_zip(client_request_timing_daily.buckets, excluded.buckets), "
            "bucket -> bucket[1] + bucket[2])",
            params,
        )
        cur.sql("COMMIT")
    finally:
        cur.close()  # an uncommitted transaction rolls back on close


def ingest(batch: list[Item]) -> int:
    """Validate and store one client batch; return the number of rows kept. Analytics is non-load-bearing."""
    if analytics_disabled():
        return 0
    today = datetime.now(UTC).date()
    rows = [row for item in batch[:MAX_ROWS_PER_BATCH] if (row := validate_row(item, today)) is not None]
    try:
        from infra.analytics_db import get_db

        write_rows(get_db(), rows)
    except Exception as error:  # analytics is intentionally non-load-bearing
        log.warning("client request timing write failed; dropped %d rows: %s", len(rows), error)
        return 0
    return len(rows)


def query_client_timing(conn, days: int) -> list[tuple]:
    """Return the client timing rows for the inclusive UTC day window."""
    from infra.request_timing import window_start

    return conn.execute(
        "SELECT day, engine, method, route, measure, count, sum_ms, max_ms, buckets "
        "FROM client_request_timing_daily WHERE day >= ?", (window_start(days),),
    ).fetchall()
