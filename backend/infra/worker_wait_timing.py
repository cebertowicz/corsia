"""Best-effort, aggregated worker queue wait telemetry.

Call record_worker_wait on the service event loop. No task or database is opened
until the first enabled recording. Call shutdown() before closing analytics at
service shutdown; abrupt termination or a failed write can lose pending data.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime, timedelta

from infra.product_events import analytics_disabled
from infra.request_timing import Aggregate

log = logging.getLogger(__name__)
_OUTCOMES = frozenset({"assigned", "cancelled", "timed_out", "failed"})
_FLUSH_SECONDS = 60
# Retain the boundary day and newer; pruning occurs on enabled flushes.
RETENTION_DAYS = 90
_MAX_PENDING_KEYS = 128
_pending: dict[tuple[str, str], Aggregate] = {}
_task: asyncio.Task | None = None
_stop: asyncio.Event | None = None
_flush_lock: asyncio.Lock | None = None
flush_failures = 0


def record_worker_wait(wait_seconds: float, outcome: str) -> None:
    """Record one completed wait; this synchronous function must run on the event loop."""
    global _task, _stop, _flush_lock
    if outcome not in _OUTCOMES or not math.isfinite(wait_seconds):
        raise ValueError("invalid worker wait outcome or duration")
    if analytics_disabled():
        return
    loop = asyncio.get_running_loop()
    key = (datetime.now(UTC).date().isoformat(), outcome)
    if key not in _pending:
        if len(_pending) >= _MAX_PENDING_KEYS:
            oldest = min(_pending)
            del _pending[oldest]
            log.warning("worker wait telemetry dropped an old pending aggregate")
        _pending[key] = Aggregate()
    _pending[key].add(max(0.0, wait_seconds * 1_000))
    if _task is None or _task.done():
        _stop = asyncio.Event()
        _flush_lock = asyncio.Lock()
        _task = loop.create_task(_flush_loop(_stop), name="worker-wait-timing-flush")


def _write_batch(batch: dict[tuple[str, str], Aggregate]) -> None:
    from infra.analytics_db import get_db

    cur = get_db().cursor()
    try:
        cur.sql("BEGIN TRANSACTION")
        values = ", ".join("(?, ?, ?, ?, ?, ?)" for _ in batch)
        params = [value for (day, outcome), aggregate in batch.items()
                  for value in (day, outcome, aggregate.count, aggregate.sum_ms,
                                aggregate.max_ms, aggregate.buckets)]
        if batch:
            cur.execute(
                "INSERT INTO worker_wait_daily (day, outcome, count, sum_ms, max_ms, buckets) VALUES " + values + " "
                "ON CONFLICT (day, outcome) DO UPDATE SET "
                "count = worker_wait_daily.count + excluded.count, "
                "sum_ms = worker_wait_daily.sum_ms + excluded.sum_ms, "
                "max_ms = GREATEST(worker_wait_daily.max_ms, excluded.max_ms), "
                "buckets = list_transform(list_zip(worker_wait_daily.buckets, excluded.buckets), "
                "bucket -> bucket[1] + bucket[2])", params,
            )
        cutoff = (datetime.now(UTC) - timedelta(days=RETENTION_DAYS)).date()
        cur.execute("DELETE FROM worker_wait_daily WHERE day < ?", (cutoff,))
        cur.sql("COMMIT")
    finally:
        cur.close()


async def flush_pending() -> None:
    """Write a bounded batch off-loop; on failure log and drop it, as for HTTP timings."""
    global _pending, flush_failures
    if _flush_lock is None:
        return
    async with _flush_lock:
        batch, _pending = _pending, {}
        if analytics_disabled() or not batch:
            return
        try:
            await asyncio.to_thread(_write_batch, batch)
        except Exception as error:
            flush_failures += 1
            log.warning("worker wait timing flush failed; dropped %d aggregates: %s", len(batch), error)


async def _flush_loop(stop: asyncio.Event) -> None:
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=_FLUSH_SECONDS)
        except TimeoutError:
            await flush_pending()
        else:
            await flush_pending()
            return


async def shutdown() -> None:
    """Stop the lazy flusher and persist pending data before analytics closes."""
    global _task, _stop, _flush_lock
    task = _task
    if task is None:
        return
    assert _stop is not None  # The task and its stop event are created together.
    _stop.set()
    await task
    _task = None
    _stop = None
    _flush_lock = None
