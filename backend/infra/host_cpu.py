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

# cpu_times() counts 1/SC_CLK_TCK-second ticks per CPU. os.sysconf('SC_CLK_TCK') reads
# 100 on this macOS arm64 host (and 100 USER_HZ is the usual Linux value), so a W-second
# window on C logical cores holds about 100 * W * C ticks and the smallest move a
# reading can show is 100 / (100 * W * C) percent. Both windows below keep that step at
# or under 1% on a 4-core host.

# With no previous snapshot the first reading measures a window itself: 0.25 s on 4
# cores is 100 ticks, so that first figure still resolves 1% steps (0.1 s would show
# only 40 ticks, a 2.5% step). Callers run this off the event loop (asyncio.to_thread),
# so the wait is affordable here and would not be in a handler's own thread.
FIRST_WINDOW_SECONDS = 0.25

# Pages poll independently, so two readings can land milliseconds apart, and a window
# with no ticks in it reads as a meaningless 0 or 100. 0.5 s on 4 cores is 200 ticks,
# so an in-window repeat still costs at most a 0.5% step instead of that noise.
MIN_WINDOW_SECONDS = 0.5

_lock = threading.Lock()
_prev_times: Any = None
_prev_monotonic = 0.0
_prev_pct = 0.0


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


def _busy_pct(previous: Any, current: Any) -> float:
    """Return the whole-host busy share between two cpu_times() snapshots."""
    prev_total, prev_idle = _totals(previous)
    total, idle = _totals(current)
    total -= prev_total
    idle -= prev_idle
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, (total - idle) / total * 100))


def usage_pct() -> float:
    """Return the whole-host busy CPU share since the previous reading.

    The window ends now and starts at the previous reading, whichever worker thread
    took it. With no reading yet it samples FIRST_WINDOW_SECONDS of host activity.
    A window shorter than MIN_WINDOW_SECONDS is too coarse to report, so the previous
    figure is repeated and that window stays open rather than being replaced.
    """
    global _prev_times, _prev_monotonic, _prev_pct
    with _lock:
        now = time.monotonic()
        if _prev_times is None:
            previous = psutil.cpu_times()
            time.sleep(FIRST_WINDOW_SECONDS)
            current, now = psutil.cpu_times(), time.monotonic()
        elif now - _prev_monotonic < MIN_WINDOW_SECONDS:
            return _prev_pct
        else:
            previous, current = _prev_times, psutil.cpu_times()

        pct = _busy_pct(previous, current)
        _prev_times, _prev_monotonic, _prev_pct = current, now, pct
        return pct
