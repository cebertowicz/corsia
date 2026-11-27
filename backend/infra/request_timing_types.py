"""Shared request timing records."""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class DbTiming:
    """Request-local database totals and worker wait, in milliseconds."""

    queue_ms: float = 0.0
    execution_ms: float = 0.0
    read_queue_ms: float = 0.0
    read_execution_ms: float = 0.0
    bulk_queue_ms: float = 0.0
    bulk_execution_ms: float = 0.0
    worker_wait_ms: float | None = None


@dataclass(frozen=True)
class SlowRequest:
    """Privacy-safe timing details retained in memory until the next batch flush."""

    ts: datetime
    method: str
    route: str
    status: int
    duration_ms: float
    dbq_ms: float
    db_ms: float
    response_bytes: int | None = None
    dbq_read_ms: float | None = None
    db_read_ms: float | None = None
    dbq_bulk_ms: float | None = None
    db_bulk_ms: float | None = None
    worker_wait_ms: float | None = None
