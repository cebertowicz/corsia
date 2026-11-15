"""Seconds each step of one lesson-report call took, for that call's log line."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
import time
from typing import ContextManager


class PhaseTimes(dict[str, float]):
    """Named phase -> seconds; a phase entered twice adds up."""

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self[name] = self.get(name, 0.0) + time.perf_counter() - started


def timed(phases: PhaseTimes | None, name: str) -> ContextManager[None]:
    """Time ``name`` into ``phases``, or do nothing when the caller keeps no times."""
    return phases.phase(name) if phases is not None else nullcontext()
