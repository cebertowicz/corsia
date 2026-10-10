"""Durable retry pacing: the ladder, the floor, jitter, and due-time reads.

These replace the tests that existed only to defend the in-memory module's
lifetimes. The pacing functions are pure, so there is no loop, no registry, and
no eviction to test — which is the point of the design.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.delivery_pacing import (
    IMMEDIATE_RETRY_FLOOR_SECONDS,
    MAX_JITTER_SECONDS,
    backoff_delay_seconds,
    is_attempt_due,
    next_attempt_after_failure,
    next_attempt_after_liveness,
    seconds_until,
    thread_jitter_seconds,
)

_T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _at(seconds: float) -> datetime:
    return _T0 + timedelta(seconds=seconds)


@pytest.mark.parametrize(
    "elapsed,expected",
    [
        (0.0, 1.0),
        (0.9, 1.0),
        (1.0, 2.0),
        (2.9, 2.0),
        (3.0, 5.0),
        (7.9, 5.0),
        (8.0, 15.0),
        (22.9, 15.0),
        (23.0, 30.0),
        (52.9, 30.0),
        (53.0, 60.0),
        (600.0, 60.0),
        (86_400.0, 60.0),
    ],
)
def test_ladder_climbs_from_elapsed_time_then_holds(elapsed, expected):
    """1→2→5→15→30→60s, then 60s forever, derived from elapsed blocked time."""
    assert backoff_delay_seconds(_T0.isoformat(), now=_at(elapsed)) == expected


def test_a_first_failure_starts_the_clock_and_later_ones_keep_it():
    """The ladder climbs because the start survives, not because we count."""
    started, first = next_attempt_after_failure("t", None, now=_T0)
    assert started == _T0.isoformat()

    # Ten seconds later the same wait is on a higher rung.
    still, second = next_attempt_after_failure("t", started, now=_at(10))
    assert still == started
    assert _parse(second) - _at(10) >= timedelta(seconds=15)
    assert _parse(first) - _T0 >= timedelta(seconds=1)


def test_liveness_restarts_the_ladder_at_the_floor():
    """Reaching the provider means the next wait begins again from now."""
    started, due = next_attempt_after_liveness("t", now=_at(500))

    assert started == _at(500).isoformat()
    delay = (_parse(due) - _at(500)).total_seconds()
    assert IMMEDIATE_RETRY_FLOOR_SECONDS <= delay <= (
        IMMEDIATE_RETRY_FLOOR_SECONDS + MAX_JITTER_SECONDS
    )


def test_liveness_never_re_probes_below_the_floor():
    """A burst of signals must not hammer a provider that is still hurting."""
    for _ in range(20):
        _started, due = next_attempt_after_liveness("t", now=_T0)
        assert (_parse(due) - _T0).total_seconds() >= IMMEDIATE_RETRY_FLOOR_SECONDS


def test_jitter_is_stable_per_thread_and_bounded():
    """One outage blocks every thread; they must not all come due together."""
    offsets = {thread_jitter_seconds(f"thread-{i}") for i in range(200)}

    assert all(0.0 <= offset <= MAX_JITTER_SECONDS for offset in offsets)
    # Spread rather than a single instant, so the herd does not stampede.
    assert len(offsets) > 100
    # Stable: the same thread always lands in the same slot.
    assert thread_jitter_seconds("thread-7") == thread_jitter_seconds("thread-7")


def test_two_threads_blocked_together_do_not_come_due_together():
    """The whole point of jitter, asserted end to end."""
    _s1, first = next_attempt_after_failure("thread-a", None, now=_T0)
    _s2, second = next_attempt_after_failure("thread-b", None, now=_T0)

    assert first != second


def test_due_and_remaining_time_read_the_schedule():
    assert is_attempt_due(_at(5).isoformat(), now=_at(6)) is True
    assert is_attempt_due(_at(5).isoformat(), now=_at(4)) is False
    assert seconds_until(_at(5).isoformat(), now=_at(4)) == pytest.approx(1.0)
    assert seconds_until(_at(5).isoformat(), now=_at(9)) == 0.0


def test_absent_or_unparseable_schedule_is_treated_as_due():
    """A head with no usable schedule must never be stranded by pacing."""
    assert is_attempt_due(None) is True
    assert is_attempt_due("not-a-timestamp") is True
    assert seconds_until(None) == 0.0


def test_a_naive_timestamp_is_read_as_utc():
    """Legacy rows may lack an offset; they must not skew the ladder."""
    naive = _T0.replace(tzinfo=None).isoformat()
    assert backoff_delay_seconds(naive, now=_at(10)) == 15.0


def _parse(moment: str) -> datetime:
    parsed = datetime.fromisoformat(moment)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
