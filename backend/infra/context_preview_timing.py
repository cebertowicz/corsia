"""Content-free stage timing for the next-turn context preview route."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import json
from threading import Lock
import time
from typing import Any, Callable, ClassVar, Iterator

from loguru import logger


_MAX_REPORTED_WAIT_MS = 60_000.0
_SUMMARY_INTERVAL_SECONDS = 300.0
_SUMMARY_BUCKETS = (
    (100.0, "le_100ms"),
    (500.0, "le_500ms"),
    (1_000.0, "le_1s"),
    (5_000.0, "le_5s"),
    (15_000.0, "le_15s"),
    (30_000.0, "le_30s"),
    (60_000.0, "le_60s"),
)
_projection_lock_wait_recorder: ContextVar[Callable[[float], None] | None] = ContextVar(
    "projection_lock_wait_recorder", default=None,
)


def record_projection_lock_wait(wait_seconds: float) -> None:
    """Record an active preview's projection flock wait, if it has one."""
    recorder = _projection_lock_wait_recorder.get()
    if recorder is not None:
        recorder(wait_seconds)


def timed(timing: "ContextPreviewTiming | None", name: str, fn: Callable[..., Any], *args, **kwargs):
    """Run ``fn`` as stage ``name`` when timing is on; otherwise just run it."""
    if timing is None:
        return fn(*args, **kwargs)
    return timing.measure(name, fn, *args, **kwargs)


def _empty_summary_buckets() -> dict[str, int]:
    return {name: 0 for _upper, name in _SUMMARY_BUCKETS} | {"gt_60s": 0}


def _summary_bucket(total_ms: float) -> str:
    for upper_bound, name in _SUMMARY_BUCKETS:
        if total_ms <= upper_bound:
            return name
    return "gt_60s"


@dataclass
class ContextPreviewSummary:
    """Keep a fixed-size, periodic preview denominator for production logs."""

    interval_seconds: float = _SUMMARY_INTERVAL_SECONDS
    last_emitted_at: float = field(default_factory=time.monotonic)
    count: int = 0
    buckets: dict[str, int] = field(default_factory=_empty_summary_buckets)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def record(self, total_ms: float, *, now: float | None = None) -> dict[str, Any] | None:
        """Count one preview and return one summary only at the fixed interval."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self.count += 1
            self.buckets[_summary_bucket(total_ms)] += 1
            elapsed_seconds = now - self.last_emitted_at
            if elapsed_seconds < self.interval_seconds:
                return None
            payload = {
                "event": "context_preview_summary",
                "interval_seconds": round(elapsed_seconds, 3),
                "count": self.count,
                "total_ms_buckets": self.buckets.copy(),
            }
            self.count = 0
            self.buckets = _empty_summary_buckets()
            self.last_emitted_at = now
            return payload


@dataclass
class ContextPreviewTiming:
    """Collect route-stage wall and CPU time, then log only slow previews."""

    thread_id: str
    threshold_ms: float = 1_000
    started_at: float = field(default_factory=time.perf_counter)
    stages: dict[str, dict[str, float]] = field(default_factory=dict)
    executor_queue_delay_ms: float = 0.0
    projection_lock_wait_ms: float = 0.0

    _summary: ClassVar[ContextPreviewSummary] = ContextPreviewSummary()

    def measure(self, name: str, fn: Callable[..., Any], *args, **kwargs):
        """Run one synchronous stage in its actual executing thread."""
        wall_started = time.perf_counter()
        cpu_started = time.thread_time()
        try:
            return fn(*args, **kwargs)
        finally:
            self.stages[name] = {
                "wall_ms": round((time.perf_counter() - wall_started) * 1_000, 3),
                "cpu_ms": round((time.thread_time() - cpu_started) * 1_000, 3),
            }

    def wrap(
        self, name: str, fn: Callable[..., Any], *, submitted_at: float | None = None,
    ) -> Callable[..., Any]:
        """Return a DB-executor-safe callback that retains original metadata."""
        @wraps(fn)
        def measured(*args, **kwargs):
            if submitted_at is not None:
                self.executor_queue_delay_ms = _bounded_milliseconds(
                    time.perf_counter() - submitted_at,
                )
            with self.capture_projection_lock_wait():
                return self.measure(name, fn, *args, **kwargs)

        return measured

    @contextmanager
    def capture_projection_lock_wait(self) -> Iterator[None]:
        """Attribute any nested projection flock wait to this preview only."""
        token = _projection_lock_wait_recorder.set(self.record_projection_lock_wait)
        try:
            yield
        finally:
            _projection_lock_wait_recorder.reset(token)

    def record_projection_lock_wait(self, wait_seconds: float) -> None:
        """Accumulate bounded flock wait across projection calls in one preview."""
        self.projection_lock_wait_ms = min(
            _MAX_REPORTED_WAIT_MS,
            self.projection_lock_wait_ms + _bounded_milliseconds(wait_seconds),
        )

    def emit_if_slow(self, preview: dict[str, Any]) -> None:
        """Emit a bounded denominator and one diagnostic line for a slow route."""
        total_ms = (time.perf_counter() - self.started_at) * 1_000
        summary = self._summary.record(total_ms)
        if summary is not None:
            logger.info("context_preview_summary {}", json.dumps(summary, separators=(",", ":")))
        if total_ms < self.threshold_ms:
            return
        payload = {
            "event": "context_preview_timing",
            "thread_id": self.thread_id,
            "total_ms": round(total_ms, 3),
            "message_count": int(preview.get("history_messages") or 0),
            "token_estimate": int(preview.get("total_used") or 0),
            "executor_queue_delay_ms": self.executor_queue_delay_ms,
            "projection_lock_wait_ms": self.projection_lock_wait_ms,
            "stages": self.stages,
        }
        logger.info("context_preview_timing {}", json.dumps(payload, separators=(",", ":")))


def _bounded_milliseconds(seconds: float) -> float:
    """Make diagnostic values finite, non-negative, and content-free."""
    return round(min(_MAX_REPORTED_WAIT_MS, max(0.0, seconds * 1_000)), 3)
