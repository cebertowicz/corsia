"""Non-blocking, aggregate-only HTTP request timing instrumentation."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Callable

from infra.product_events import analytics_disabled

log = logging.getLogger(__name__)

_BUCKET_UPPERS = (1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1_024, 2_048, 4_096,
                  8_192, 16_384, 32_768, 60_000)
_FLUSH_SECONDS = 60
_SLOW_MS = 1_000
_UNMATCHED_ROUTE = "<unmatched>"
_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


@dataclass
class DbTiming:
    """Mutable request-local database timing totals in milliseconds.

    ``queue_ms`` and ``execution_ms`` total every executor lane. The read and
    bulk lanes are also kept apart, so a slow request shows which lane it waited on.
    """

    queue_ms: float = 0.0
    execution_ms: float = 0.0
    read_queue_ms: float = 0.0
    read_execution_ms: float = 0.0
    bulk_queue_ms: float = 0.0
    bulk_execution_ms: float = 0.0


@dataclass
class Aggregate:
    """One unflushed route/status timing aggregate."""

    count: int = 0
    sum_ms: float = 0.0
    max_ms: float = 0.0
    buckets: list[int] = field(default_factory=lambda: [0] * len(_BUCKET_UPPERS))

    def add(self, duration_ms: float) -> None:
        """Add one completed request duration to this aggregate."""
        self.count += 1
        self.sum_ms += duration_ms
        self.max_ms = max(self.max_ms, duration_ms)
        for index, upper in enumerate(_BUCKET_UPPERS):
            if duration_ms <= upper:
                self.buckets[index] += 1
                return
        self.buckets[-1] += 1


@dataclass(frozen=True)
class SlowRequest:
    """Aggregate-safe details retained only until the next batch flush."""

    ts: datetime
    method: str
    route: str
    status: int
    duration_ms: float
    dbq_ms: float
    db_ms: float
    dbq_read_ms: float | None = None
    db_read_ms: float | None = None
    dbq_bulk_ms: float | None = None
    db_bulk_ms: float | None = None


_request_db_timing: contextvars.ContextVar[DbTiming | None] = contextvars.ContextVar(
    "request_db_timing", default=None
)
_aggregates: dict[tuple[str, str, str, str], Aggregate] = defaultdict(Aggregate)
_slow_requests: list[SlowRequest] = []
flush_failures = 0


def add_db_timing(queue_seconds: float, execution_seconds: float, lane: str | None = None) -> None:
    """Accumulate one database-pool job into the active HTTP request, if any."""
    timing = _request_db_timing.get()
    if timing is None:
        return
    queue_ms = max(0.0, queue_seconds * 1_000)
    execution_ms = max(0.0, execution_seconds * 1_000)
    timing.queue_ms += queue_ms
    timing.execution_ms += execution_ms
    if lane == "read":
        timing.read_queue_ms += queue_ms
        timing.read_execution_ms += execution_ms
    elif lane == "bulk":
        timing.bulk_queue_ms += queue_ms
        timing.bulk_execution_ms += execution_ms


def bucket_percentile(buckets: list[int], percentile: float) -> float:
    """Estimate a percentile from fixed-width log histogram buckets in ms."""
    total = sum(buckets)
    if total == 0:
        return 0.0
    target = total * percentile
    cumulative = 0
    lower = 0.0
    for upper, count in zip(_BUCKET_UPPERS, buckets, strict=True):
        if cumulative + count >= target and count:
            fraction = (target - cumulative) / count
            return lower + (upper - lower) * fraction
        cumulative += count
        lower = float(upper)
    return float(_BUCKET_UPPERS[-1])


def route_template(scope: dict) -> str:
    """Return the normalized matched route template, never a client path."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else _UNMATCHED_ROUTE


def _normalized_method(method: object) -> str:
    """Return one bounded HTTP method label for aggregate keys."""
    method = str(method).upper()
    return method if method in _HTTP_METHODS else "OTHER"


def _server_timing(started_at: float, timing: DbTiming) -> bytes:
    app_ms = (time.perf_counter() - started_at) * 1_000
    return (
        f"app;dur={app_ms:.1f}, dbq;dur={timing.queue_ms:.1f}, "
        f"db;dur={timing.execution_ms:.1f}, "
        f"dbq_read;dur={timing.read_queue_ms:.1f}, db_read;dur={timing.read_execution_ms:.1f}, "
        f"dbq_bulk;dur={timing.bulk_queue_ms:.1f}, db_bulk;dur={timing.bulk_execution_ms:.1f}"
    ).encode()


class RequestTimingMiddleware:
    """Measure HTTP completion without buffering response bodies or streams."""

    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started_at = time.perf_counter()
        timing = DbTiming()
        token = _request_db_timing.set(timing)
        status = 500
        response_started = False
        recorded = False

        async def timed_send(message) -> None:
            nonlocal status, response_started, recorded
            message_type = message["type"]
            if message_type == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", ()))
                headers.append((b"server-timing", _server_timing(started_at, timing)))
                message = {**message, "headers": headers}
                response_started = True
            elif message_type == "http.response.body" and not message.get("more_body", False):
                if response_started and not recorded:
                    self._record(scope, started_at, status, timing)
                    recorded = True
            await send(message)

        try:
            await self.app(scope, receive, timed_send)
        except Exception:
            # Unhandled errors reach ServerErrorMiddleware outside this one, so
            # its 500 response never passes through timed_send; count it here.
            if not recorded:
                self._record(scope, started_at, status, timing)
            raise
        finally:
            _request_db_timing.reset(token)

    @staticmethod
    def _record(scope: dict, started_at: float, status: int, timing: DbTiming) -> None:
        duration_ms = (time.perf_counter() - started_at) * 1_000
        route = route_template(scope)
        day = datetime.now(UTC).date().isoformat()
        key = (day, _normalized_method(scope.get("method")), route, f"{status // 100}xx")
        _aggregates[key].add(duration_ms)
        if duration_ms > _SLOW_MS:
            _slow_requests.append(
                SlowRequest(
                    datetime.now(UTC), key[1], route, status, duration_ms,
                    timing.queue_ms, timing.execution_ms,
                    timing.read_queue_ms, timing.read_execution_ms,
                    timing.bulk_queue_ms, timing.bulk_execution_ms,
                )
            )


_Batch = tuple[dict[tuple[str, str, str, str], Aggregate], list[SlowRequest]]


def _swap_pending() -> _Batch:
    """Detach the pending batch; called only on the event-loop thread."""
    global _aggregates, _slow_requests
    batch = (_aggregates, _slow_requests)
    _aggregates = defaultdict(Aggregate)
    _slow_requests = []
    return batch


def write_batch(aggregates: dict[tuple[str, str, str, str], Aggregate],
                slow: list[SlowRequest]) -> None:
    """Persist one detached batch in a single DuckDB transaction; raise on failure."""
    from infra.analytics_db import get_db

    cur = get_db().cursor()
    try:
        cur.sql("BEGIN")
        if aggregates:
            values = ", ".join("(?, ?, ?, ?, ?, ?, ?, ?)" for _ in aggregates)
            params = [
                value
                for (day, method, route, status_class), aggregate in aggregates.items()
                for value in (day, method, route, status_class, aggregate.count, aggregate.sum_ms,
                              aggregate.max_ms, aggregate.buckets)
            ]
            cur.execute(
                "INSERT INTO request_timing_daily VALUES " + values + " "
                "ON CONFLICT (day, method, route, status_class) DO UPDATE SET "
                "count = request_timing_daily.count + excluded.count, "
                "sum_ms = request_timing_daily.sum_ms + excluded.sum_ms, "
                "max_ms = GREATEST(request_timing_daily.max_ms, excluded.max_ms), "
                "buckets = list_transform(list_zip(request_timing_daily.buckets, excluded.buckets), "
                "bucket -> bucket[1] + bucket[2])",
                params,
            )
        if slow:
            values = ", ".join("(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)" for _ in slow)
            slow_params = [
                value
                for row in slow
                for value in (row.ts, row.method, row.route, row.status, row.duration_ms,
                              row.dbq_ms, row.db_ms, row.dbq_read_ms, row.db_read_ms,
                              row.dbq_bulk_ms, row.db_bulk_ms)
            ]
            cur.execute(
                "INSERT INTO request_slow (ts, method, route, status, dur_ms, dbq_ms, db_ms, "
                "dbq_read_ms, db_read_ms, dbq_bulk_ms, db_bulk_ms) VALUES " + values,
                slow_params,
            )
        cur.sql("COMMIT")
    finally:
        cur.close()  # an uncommitted transaction rolls back on close


async def flush_pending() -> None:
    """Swap the batch on the loop thread, then write it off-loop or drop it."""
    global flush_failures
    aggregates, slow = _swap_pending()
    if analytics_disabled() or not (aggregates or slow):
        return
    try:
        await asyncio.to_thread(write_batch, aggregates, slow)
    except Exception as error:  # analytics is intentionally non-load-bearing
        flush_failures += 1
        log.warning("request timing flush failed; dropped %d aggregate keys and %d slow rows: %s",
                    len(aggregates), len(slow), error)


async def _flush_loop() -> None:
    """Flush timing aggregates at a bounded cadence until lifespan shutdown."""
    try:
        while True:
            await asyncio.sleep(_FLUSH_SECONDS)
            await flush_pending()
    except asyncio.CancelledError:
        await flush_pending()
        raise


def start_flush_loop() -> asyncio.Task:
    """Start the single lifespan-owned timing flush task."""
    return asyncio.create_task(_flush_loop(), name="request-timing-flush")


def window_start(days: int) -> date:
    """Return the first UTC day of an inclusive window: today and days-1 before it."""
    return datetime.now(UTC).date() - timedelta(days=days - 1)


def query_route_timing(conn, days: int) -> list[tuple]:
    """Return route timing rows for the inclusive UTC day window."""
    return conn.execute(
        "SELECT day, method, route, status_class, count, max_ms, buckets "
        "FROM request_timing_daily WHERE day >= ?", (window_start(days),),
    ).fetchall()


def prune_retention(now: datetime | None = None) -> None:
    """Prune timing analytics retention windows during the existing nightly job."""
    if analytics_disabled():
        return
    now = now or datetime.now(UTC)
    cur = None
    try:
        from infra.analytics_db import get_db

        cur = get_db().cursor()
        cur.execute("DELETE FROM request_slow WHERE ts < ?", (now - timedelta(days=30),))
        cur.execute(
            "DELETE FROM request_timing_daily WHERE day < ?",
            ((now - timedelta(days=90)).date(),),
        )
    except Exception:
        log.exception("request timing analytics retention failed")
    finally:
        if cur is not None:
            cur.close()


def _reset_for_tests() -> None:
    """Clear process-local timing state between tests."""
    global flush_failures
    _swap_pending()
    flush_failures = 0
