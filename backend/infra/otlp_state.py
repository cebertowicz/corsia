"""In-memory state of the OTLP receiver: per-agent request caps, request refusal counts, the bound port.

Refused requests never write to the database (a flood must not become a write flood), so
their counts live here and are lost on restart; ``measure_status`` says so.
"""
from __future__ import annotations

import math
import threading
import time
from collections import Counter, deque
from collections.abc import Callable

REQUESTS_PER_MINUTE = 60
RECORDS_PER_MINUTE = 600
RECORDS_PER_REQUEST = 500
_WINDOW_SECONDS = 60.0

# Why a request was refused after its key resolved, for the agent's counts.
CAUSES = ("too_large", "malformed", "rate_limited", "busy")


class Caps:
    """Sliding one-minute windows per agent: requests, and records inside admitted requests."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._requests: dict[str, deque[float]] = {}
        self._records: dict[str, deque[tuple[float, int]]] = {}

    def _retry(self, oldest: float, now: float) -> int:
        return max(1, math.ceil(oldest + _WINDOW_SECONDS - now))

    def charge_request(self, agent: str) -> int | None:
        """Count one request; the seconds to wait when the agent is over its minute, else None."""
        now = self._clock()
        with self._lock:
            window = self._requests.setdefault(agent, deque())
            while window and now - window[0] >= _WINDOW_SECONDS:
                window.popleft()
            if len(window) >= REQUESTS_PER_MINUTE:
                return self._retry(window[0], now)
            window.append(now)
            return None

    def charge_records(self, agent: str, count: int) -> int | None:
        """Count ``count`` records; the seconds to wait (nothing is counted) when it would pass the minute's cap."""
        now = self._clock()
        with self._lock:
            window = self._records.setdefault(agent, deque())
            while window and now - window[0][0] >= _WINDOW_SECONDS:
                window.popleft()
            if sum(n for _, n in window) + count > RECORDS_PER_MINUTE:
                return self._retry(window[0][0], now) if window else int(_WINDOW_SECONDS)
            window.append((now, count))
            return None


class Refusals:
    """Request refusals by agent and cause, in memory only."""

    def __init__(self) -> None:
        self._counts: dict[str, Counter[str]] = {}
        self._lock = threading.Lock()

    def add(self, agent: str, cause: str) -> None:
        assert cause in CAUSES
        with self._lock:
            self._counts.setdefault(agent, Counter())[cause] += 1

    def of(self, agent: str) -> dict[str, int]:
        with self._lock:
            return dict(self._counts.get(agent, ()))

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


CAPS = Caps()
REFUSALS = Refusals()
# The loopback port the receiver is bound to, or None while it is not listening.
_bound_port: int | None = None


def set_port(port: int | None) -> None:
    global _bound_port
    _bound_port = port


def endpoint() -> str | None:
    """The base URL an exporter's ``OTEL_EXPORTER_OTLP_ENDPOINT`` takes, or None while not listening."""
    return f"http://127.0.0.1:{_bound_port}" if _bound_port is not None else None
