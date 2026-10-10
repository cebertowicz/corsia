"""Retry pacing derived from durable state, with no process-global memory.

A blocked head's timing lives on its own ``messages`` row — ``next_attempt_at``
for when to try again and ``delivery_blocked_since`` for when the wait began.
Both are written by the transactions that already own the row, so pacing cannot
outlive the message, cannot bind to an event loop, and needs no eviction.

There is no attempt counter anywhere. The ladder is a pure function of how long
the head has been continuously blocked, so "how many times have we tried" is
never asked or stored.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib

# Cumulative elapsed-wait thresholds and the delay to use once past each one.
# Reproduces 1 → 2 → 5 → 15 → 30 → 60s and then holds at 60s forever.
_LADDER: tuple[tuple[float, float], ...] = (
    (0.0, 1.0),
    (1.0, 2.0),
    (3.0, 5.0),
    (8.0, 15.0),
    (23.0, 30.0),
    (53.0, 60.0),
)

# A burst of liveness signals must not hammer a provider that is still hurting.
IMMEDIATE_RETRY_FLOOR_SECONDS = 1.0

# One provider outage blocks every thread at once, so without a spread they all
# come due on the same tick and stampede the provider at its weakest moment.
# Derived from the thread id so a given thread's offset is stable and testable.
MAX_JITTER_SECONDS = 0.25

LIVENESS_SOURCES = frozenset(
    {"provider_ack", "turn_terminal", "user_send", "reconnect", "retry_now"}
)


def thread_jitter_seconds(thread_id: str) -> float:
    """Return this thread's stable offset within the stampede window."""
    digest = hashlib.blake2b(thread_id.encode("utf-8"), digest_size=2).digest()
    return int.from_bytes(digest, "big") / 65535 * MAX_JITTER_SECONDS


def _parse(moment: str | None) -> datetime | None:
    if not moment:
        return None
    try:
        parsed = datetime.fromisoformat(moment)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def backoff_delay_seconds(blocked_since: str | None, *, now: datetime) -> float:
    """Return the delay for the next attempt, from elapsed blocked time alone."""
    started = _parse(blocked_since)
    elapsed = 0.0 if started is None else max(
        0.0, (now - started).total_seconds()
    )
    delay = _LADDER[0][1]
    for threshold, rung in _LADDER:
        if elapsed >= threshold:
            delay = rung
    return delay


def next_attempt_after_failure(
    thread_id: str,
    blocked_since: str | None,
    *,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Return the ``(blocked_since, next_attempt_at)`` for one more wait.

    The first failure starts the clock; later ones keep the original start so
    the ladder climbs. Neither value counts attempts.
    """
    moment = now or datetime.now(timezone.utc)
    started = _parse(blocked_since) or moment
    delay = backoff_delay_seconds(started.isoformat(), now=moment)
    delay += thread_jitter_seconds(thread_id)
    return started.isoformat(), (moment + timedelta(seconds=delay)).isoformat()


def next_attempt_after_liveness(
    thread_id: str, *, now: datetime | None = None,
) -> tuple[str, str]:
    """Return the ``(blocked_since, next_attempt_at)`` for a proven-live thread.

    Reaching the provider restarts the ladder, so the wait begins again from
    now — but never sooner than the floor, so a burst of signals cannot hammer
    a provider that is still struggling.
    """
    moment = now or datetime.now(timezone.utc)
    delay = IMMEDIATE_RETRY_FLOOR_SECONDS + thread_jitter_seconds(thread_id)
    return (
        moment.isoformat(),
        (moment + timedelta(seconds=delay)).isoformat(),
    )


def is_attempt_due(next_attempt_at: str | None, *, now: datetime | None = None) -> bool:
    """Return whether a blocked head's next attempt is due."""
    due = _parse(next_attempt_at)
    if due is None:
        return True
    return (now or datetime.now(timezone.utc)) >= due


def seconds_until(next_attempt_at: str | None, *, now: datetime | None = None) -> float:
    """Return how long until this attempt is due; 0 when it already is."""
    due = _parse(next_attempt_at)
    if due is None:
        return 0.0
    return max(0.0, (due - (now or datetime.now(timezone.utc))).total_seconds())
