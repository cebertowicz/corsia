"""One GitHub read budget per account, rebuilt from response headers.

GitHub's limit belongs to the account a read runs as, so each login keeps its
own: ``budget`` for the agents' GitHub account (and the locked reads made while
it is not ready) and ``person_budget`` for the person's own ``gh`` login, which
the default broker also spends while there is no agents' account. A reader
carries the budget of the login it last read as (``budget_of`` picks it); the
default broker switches budget at the read after the agents' account is added
or removed, so that one read is asked against the old budget. The event pollers
read through ``github_notifications_feed.gh_get``; script reads use
``automation.script_github_channel``. Both ask before each call and feed
response headers here. A limit one account sees backs off only that account's
reads. Script reads and the quiet-PR sweep stay out of the reserve. Other
GitHub callers (``connections.github_run.run_gh``, for example) do not consult it.
Nothing is stored: a restart relearns the budget from the next response.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Mapping

#: GitHub's documented primary limit for an authenticated user, used until a
#: response reports the account's own ``x-ratelimit-limit``.
DEFAULT_LIMIT = 5000
#: A rate-limited response without ``retry-after``: GitHub says to wait at
#: least a minute.
DEFAULT_RETRY_S = 60.0
#: Every caller reads REST core. A response for another resource says nothing
#: about core's window, so it is not learned from.
_CORE = "core"


class ReadBudget:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._limit = DEFAULT_LIMIT
        self._remaining: int | None = None
        self._reset_at: float | None = None
        self._retry_until = 0.0

    def clear(self) -> None:
        with self._lock:
            self._limit = DEFAULT_LIMIT
            self._remaining = None
            self._reset_at = None
            self._retry_until = 0.0

    def reserve(self) -> int:
        """5% of the account's hourly limit, kept for its normal reads (the
        notifications poller's, on the person's): low-priority reads must not
        spend it, and an account with a different limit (say 15,000) keeps the
        same proportion."""
        return self._limit // 20

    def record(self, status: int, headers: Mapping[str, str], body: Any = None) -> bool:
        """Learn from one response (``headers`` keys lower-case); True if it was a rate limit.

        A rate limit is a 429, or a 403 that GitHub marks as one: no requests
        remaining, a ``retry-after``, or a secondary-limit message in the body.
        """
        now = self._clock()
        remaining = _int(headers.get("x-ratelimit-remaining"))
        reset = _int(headers.get("x-ratelimit-reset"))
        retry_after = _int(headers.get("retry-after"))
        limit = _int(headers.get("x-ratelimit-limit"))
        limited = status == 429 or (status == 403 and (
            remaining == 0 or retry_after is not None or _secondary_limit(body)
        ))
        with self._lock:
            if headers.get("x-ratelimit-resource", _CORE) == _CORE:
                if limit:
                    self._limit = limit
                if remaining is not None:
                    self._remaining = remaining
                    self._reset_at = float(reset) if reset is not None else None
            if limited or retry_after is not None:
                wait = float(retry_after) if retry_after is not None else DEFAULT_RETRY_S
                self._retry_until = max(self._retry_until, now + wait)
        return limited

    def blocked_until(self, *, low_priority: bool = False) -> float | None:
        """When a read may next run, or None when it may run now."""
        now = self._clock()
        with self._lock:
            until = self._retry_until if self._retry_until > now else None
            if self._live(now) and (self._remaining or 0) < (self.reserve() if low_priority else 1):
                until = max(until or 0.0, self._reset_at or 0.0)
            return until

    def sweep_paused(self) -> bool:
        """Whether the quiet-PR sweep should stay out of the reserve right now."""
        with self._lock:
            return self._reserve_in_use(self._clock())

    def status(self) -> dict[str, Any]:
        """Remaining reads, reset time, and whether (and until when) reads back off; empty until learned."""
        now = self._clock()
        with self._lock:
            retry_until = self._retry_until if self._retry_until > now else None
            if self._remaining is None and retry_until is None:
                return {}
            exhausted_until = self._reset_at if self._live(now) and (self._remaining or 0) < 1 else None
            until = max(retry_until or 0.0, exhausted_until or 0.0) or None
            return {
                "remaining": self._remaining,
                "limit": self._limit,
                "reset_at": _iso(self._reset_at),
                "backing_off": until is not None,
                "backoff_until": _iso(until),
                "sweep_paused": self._reserve_in_use(now),
            }

    def _live(self, now: float) -> bool:
        return self._remaining is not None and self._reset_at is not None and self._reset_at > now

    def _reserve_in_use(self, now: float) -> bool:
        return self._live(now) and (self._remaining or 0) < self.reserve()


def _secondary_limit(body: Any) -> bool:
    """GitHub's secondary-limit 403 says so in the JSON body's message."""
    message = body.get("message") if isinstance(body, dict) else None
    return isinstance(message, str) and "secondary rate limit" in message.lower()


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
person_budget = ReadBudget()


def budget_of(runner: Any) -> ReadBudget:
    """The budget of the account ``runner`` reads as; a runner that doesn't say reads as the agents'."""
    return getattr(runner, "read_budget", None) or budget
