"""Bounded stage timing for protected Bash calls.

Measurement only: a call that runs longer than ``SLOW_CALL_SECONDS`` emits one
structured log line of per-stage elapsed milliseconds. The line carries stage
names, durations, and an outcome class and nothing about the call itself, so
it never includes command text, paths, identities, output, or repo names.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
import logging
import sys
import time
from typing import Iterator

from host_access.spawn_stage import SPAWN_STAGE_HOOK

log = logging.getLogger(__name__)

SLOW_CALL_SECONDS = 5.0
# Storm bound: at most this many lines per window; the excess is counted and
# reported on the next line that is emitted.
MAX_LINES_PER_WINDOW = 20
WINDOW_SECONDS = 60.0

STAGES = (
    "admission",  # host-access permit consumption and lease check
    "policy",  # repository policy load
    "guard",  # command guard, Safehouse version check, cwd resolution
    "envelope",  # execution envelope/profile preparation
    "lock_wait",  # process-owner lock wait before spawn
    "exec",  # subprocess creation and process registration
    "run",  # spawn return after exec (foreground: command runtime)
    "post",  # result assembly, artifact observation, truncation
)
OUTCOMES = frozenset({"returned", "exception", "cancelled"})

_window_start = 0.0
_window_lines = 0
_suppressed = 0


def _admit_line(now: float) -> int | None:
    """Return the suppressed count to report, or None when over budget."""
    global _window_start, _window_lines, _suppressed
    if now - _window_start >= WINDOW_SECONDS:
        _window_start = now
        _window_lines = 0
    if _window_lines >= MAX_LINES_PER_WINDOW:
        _suppressed += 1
        return None
    _window_lines += 1
    reported, _suppressed = _suppressed, 0
    return reported


class ShellStageTimer:
    """Attribute elapsed time since the previous mark to the named stage."""

    __slots__ = ("_start", "_last", "_elapsed", "_last_stage", "failed")

    def __init__(self) -> None:
        # Set by the caller when it handles an exception and returns.
        self.failed = False
        self._start = self._last = time.perf_counter()
        self._elapsed: dict[str, float] = {}
        self._last_stage = "none"

    def mark(self, stage: str) -> None:
        """Attribute time since the previous mark to ``stage``."""
        now = time.perf_counter()
        self._elapsed[stage] = self._elapsed.get(stage, 0.0) + (now - self._last)
        self._last = now
        self._last_stage = stage

    @contextmanager
    def spawn_window(self) -> Iterator[None]:
        """Route ProcessOwner.spawn stage callbacks to this timer."""
        token = SPAWN_STAGE_HOOK.set(self.mark)
        try:
            yield
        finally:
            SPAWN_STAGE_HOOK.reset(token)

    def emit(self, outcome: str | None = None) -> None:
        """Log the stage line for a slow call; never raises.

        Called from a ``finally``: with no explicit outcome it is read from the
        exception in flight (cancel or error) or from ``failed``.
        """
        # Observation is best-effort: a failed clock or log sink cannot change Bash.
        with suppress(Exception):
            if outcome is None:
                in_flight = sys.exc_info()[1]
                if isinstance(in_flight, asyncio.CancelledError):
                    outcome = "cancelled"
                elif in_flight is not None or self.failed:
                    outcome = "exception"
                else:
                    outcome = "returned"
            now = time.perf_counter()
            total = now - self._start
            if total < SLOW_CALL_SECONDS:
                return
            reported = _admit_line(now)
            if reported is None:
                return
            if outcome not in OUTCOMES:
                outcome = "exception"
            fields = [
                f"outcome={outcome}",
                f"last_stage={self._last_stage}",
                f"total_ms={round(total * 1000)}",
            ]
            for stage in STAGES:
                fields.append(f"{stage}_ms={round(self._elapsed.get(stage, 0.0) * 1000)}")
            # Time after the last mark: where an abort or cancel was waiting.
            fields.append(f"unmarked_ms={round((now - self._last) * 1000)}")
            if reported:
                fields.append(f"suppressed={reported}")
            log.warning("bash_stage_timing %s", " ".join(fields))
