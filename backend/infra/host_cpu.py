"""Whole-host CPU usage, measured between polls without a subprocess.

psutil.cpu_percent(interval=None) is the obvious call and it is wrong here: it keys
its baseline by thread id, and the system-status handler runs its collection on
whichever default-executor thread asyncio.to_thread picks. Each new worker thread
would read a meaningless 0.0 and later ones would average an arbitrary window. So
keep the previous psutil.cpu_times() snapshot in module state, guarded by a lock,
and report the busy share between that snapshot and the current one.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import psutil

# The first reading has no previous snapshot to compare against, so it measures a
# short window itself. Callers run this off the event loop (asyncio.to_thread), which
# is why the wait is affordable here and would not be in a request handler's own thread.
FIRST_WINDOW_SECONDS = 0.1

_lock = threading.Lock()
_prev_times: Any = None


def _totals(times: Any) -> tuple[float, float]:
    """Return (total, idle) CPU seconds in one cpu_times() snapshot.

    iowait is idle time that Linux keeps out of "idle", and Linux counts guest time
    inside user as well so it is not extra total time (both per psutil and htop).
    Fields are read by name because the two platforms report different ones.
    """
    total = (
        sum(times)
        - getattr(times, "guest", 0.0)
        - getattr(times, "guest_nice", 0.0)
    )
    idle = getattr(times, "idle", 0.0) + getattr(times, "iowait", 0.0)
    return total, idle


def usage_pct() -> float:
    """Return the whole-host busy CPU share since the previous reading.

    The window ends now and starts at the previous reading, whichever worker thread
    took it. With no reading yet it samples FIRST_WINDOW_SECONDS of host activity.
    """
    global _prev_times
    with _lock:
        first = psutil.cpu_times()
        if _prev_times is None:
            time.sleep(FIRST_WINDOW_SECONDS)
            current, previous = psutil.cpu_times(), first
        else:
            previous, current = _prev_times, first
        _prev_times = current

    prev_total, prev_idle = _totals(previous)
    total, idle = _totals(current)
    total -= prev_total
    idle -= prev_idle
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, (total - idle) / total * 100))
