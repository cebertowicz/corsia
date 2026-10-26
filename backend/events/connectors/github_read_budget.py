"""One process-wide GitHub read budget, rebuilt from response headers.

Every poller reads through ``github_notifications_feed.gh_get``, which feeds each
response's rate-limit headers here and asks before each call. A limit seen by
one reader therefore backs off all of them, so the person's GitHub limit is
never burned. Nothing is stored: a restart relearns the budget from the next
response.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

#: GitHub's documented primary limit for an authenticated user is 5,000 core
#: requests an hour; keep 5% of it for the notifications poller, which the
#: lower-priority quiet-PR sweep must not spend.
RESERVE = 250
#: A 429 or secondary-limit 403 without ``retry-after``: GitHub says to wait
#: at least a minute.
DEFAULT_RETRY_S = 60.0
_CORE = "core"


@dataclass(slots=True)
class _Window:
    remaining: int
    reset_at: float | None


class ReadBudget:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: dict[str, _Window] = {}
        self._retry_until = 0.0

    def clear(self) -> None:
        with self._lock:
            self._windows.clear()
            self._retry_until = 0.0

    def record(self, status: int, headers: Mapping[str, str]) -> None:
        """Learn from one response; ``headers`` keys are lower-case."""
        now = self._clock()
        resource = headers.get("x-ratelimit-resource") or _CORE
        remaining = _int(headers.get("x-ratelimit-remaining"))
        reset = _int(headers.get("x-ratelimit-reset"))
        retry_after = _int(headers.get("retry-after"))
        with self._lock:
            if remaining is not None:
                self._windows[resource] = _Window(remaining, float(reset) if reset is not None else None)
            if retry_after is not None or status == 429:
                wait = float(retry_after) if retry_after is not None else DEFAULT_RETRY_S
                self._retry_until = max(self._retry_until, now + wait)

    def blocked_until(self, resource: str = _CORE, *, low_priority: bool = False) -> float | None:
        """When a read on ``resource`` may next run, or None when it may run now."""
        now = self._clock()
        with self._lock:
            until = self._retry_until if self._retry_until > now else None
            window = self._windows.get(resource)
            if window is not None and window.reset_at is not None and window.reset_at > now:
                if window.remaining < (RESERVE if low_priority else 1):
                    until = max(until or 0.0, window.reset_at)
            return until

    def status(self) -> dict[str, Any]:
        """Per resource: remaining, reset time, and whether (and until when) reads back off."""
        now = self._clock()
        with self._lock:
            retry_until = self._retry_until if self._retry_until > now else None
            resources: dict[str, Any] = {}
            for name, window in self._windows.items():
                live = window.reset_at is not None and window.reset_at > now
                exhausted_until = window.reset_at if live and window.remaining < 1 else None
                until = max(retry_until or 0.0, exhausted_until or 0.0) or None
                resources[name] = {
                    "remaining": window.remaining,
                    "reset_at": _iso(window.reset_at),
                    "backing_off": until is not None,
                    "backoff_until": _iso(until),
                    "sweep_paused": live and window.remaining < RESERVE,
                }
            if retry_until is not None and _CORE not in resources:
                resources[_CORE] = {
                    "remaining": None, "reset_at": None, "backing_off": True,
                    "backoff_until": _iso(retry_until), "sweep_paused": True,
                }
            return resources


def resource_for(path: str) -> str:
    """The rate-limit resource a REST path draws on."""
    head = path.lstrip("/").split("?", 1)[0].split("/", 1)[0]
    return head if head in ("search", "graphql") else _CORE


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _iso(moment: float | None) -> str | None:
    if moment is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))


budget = ReadBudget()
