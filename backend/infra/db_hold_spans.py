"""Temporary inclusive timings for the current thread's write hold."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator

_local = threading.local()


def begin() -> dict[str, tuple[int, float]]:
    spans: dict[str, tuple[int, float]] = {}
    stack = getattr(_local, "stack", None)
    if stack is None:
        stack = _local.stack = []
    stack.append(spans)
    return spans


def end() -> None:
    _local.stack.pop()


@contextmanager
def span(name: str) -> Iterator[None]:
    stack = getattr(_local, "stack", ())
    if not stack:
        yield
        return
    spans = stack[-1]
    started = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - started
        count, total = spans.get(name, (0, 0.0))
        spans[name] = (count + 1, total + elapsed)


def format_spans(spans: dict[str, tuple[int, float]]) -> str:
    return ",".join(f"{name}:{count}/{seconds:.3f}s" for name, (count, seconds) in sorted(spans.items())) or "-"
