"""Worker wait recording and persistence within the request timing batch."""

from __future__ import annotations

from datetime import UTC, datetime

from infra.request_timing_types import SlowRequest

_WORKER_OUTCOMES = {"assigned": 200, "cancelled": 499, "timed_out": 408, "failed": 500}


def record_worker_wait(wait_seconds: float, outcome: str, worker_waits, slow_requests, timing) -> None:
    """Record one pool wait in the caller's current request timing batch."""
    if outcome not in _WORKER_OUTCOMES:
        raise ValueError("invalid worker wait outcome")
    wait_ms = max(0.0, wait_seconds * 1_000)
    now = datetime.now(UTC)
    worker_waits[(now.date().isoformat(), outcome)].add(wait_ms)
    slow_requests.append(SlowRequest(
        now, "WORKER", "<worker-pool>", _WORKER_OUTCOMES[outcome], wait_ms, 0, 0,
        worker_wait_ms=wait_ms,
    ))
    if timing is not None:
        timing.worker_wait_ms = (timing.worker_wait_ms or 0.0) + wait_ms


def write_worker_waits(cur, worker_waits) -> None:
    """Write worker aggregates inside the caller's request-timing transaction."""
    if not worker_waits:
        return
    values = ", ".join("(?, ?, ?, ?, ?, ?)" for _ in worker_waits)
    params = [value for (day, outcome), aggregate in worker_waits.items()
              for value in (day, outcome, aggregate.count, aggregate.sum_ms,
                            aggregate.max_ms, aggregate.buckets)]
    cur.execute(
        "INSERT INTO worker_wait_daily (day, outcome, count, sum_ms, max_ms, buckets) VALUES " + values + " "
        "ON CONFLICT (day, outcome) DO UPDATE SET "
        "count = worker_wait_daily.count + excluded.count, "
        "sum_ms = worker_wait_daily.sum_ms + excluded.sum_ms, "
        "max_ms = GREATEST(worker_wait_daily.max_ms, excluded.max_ms), "
        "buckets = list_transform(list_zip(worker_wait_daily.buckets, excluded.buckets), "
        "bucket -> bucket[1] + bucket[2])", params,
    )
