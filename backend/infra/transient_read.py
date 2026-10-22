"""Bounded retry for synchronous reads that temporarily return ``None``."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar


_T = TypeVar("_T")
TRANSIENT_READ_ATTEMPTS = 8
TRANSIENT_READ_INTERVAL_S = 4.0


def retry_transient_read(
    read: Callable[[], _T | None],
    *,
    attempts: int,
    interval_s: float,
    log_label: Callable[[], str],
    log_retry: Callable[[str], None],
) -> _T | None:
    """Return the first readable result, or ``None`` after a bounded re-poll.

    ``log_label`` is deferred because a caller may learn why its read is
    transient only after an individual attempt. ``log_retry`` keeps the helper
    independent of a logging implementation while preserving caller context.
    """
    for attempt in range(attempts):
        result = read()
        if result is not None:
            return result
        if attempt + 1 < attempts:
            log_retry(f"{log_label()}, re-polling ({attempt + 1}/{attempts})")
            time.sleep(interval_s)
    return None
