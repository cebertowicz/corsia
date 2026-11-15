"""Process-local totals for the main SQLite write lock."""

import logging
import threading
import time

log = logging.getLogger(__name__)


class WriteLockWindow:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._reset()

    def _reset(self) -> None:
        self.writes = 0
        self.held = 0.0
        self.short_held = 0.0
        self.max_held = 0.0
        self.waited = 0.0
        self.waited_writes = 0
        self.max_waited = 0.0
        self.scopes: dict[str, tuple[int, float]] = {}

    def wait(self, seconds: float) -> None:
        with self._lock:
            self.waited += seconds
            self.waited_writes += seconds >= 0.05
            self.max_waited = max(self.max_waited, seconds)

    def hold(self, scope: str, seconds: float, short: bool) -> None:
        with self._lock:
            self.writes += 1
            self.held += seconds
            if short:
                self.short_held += seconds
            self.max_held = max(self.max_held, seconds)
            count, held = self.scopes.get(scope, (0, 0.0))
            self.scopes[scope] = count + 1, held + seconds
            now = time.monotonic()
            elapsed = now - self._started
            if elapsed < 60:
                return
            scopes = sorted(self.scopes.items(), key=lambda item: item[1][1], reverse=True)[:5]
            top = "; ".join(f"{name} {count}/{held:.2f}s" for name, (count, held) in scopes)
            # WARNING is required for the served process; see cpu_sampler.emit_summary.
            log.warning(
                "SQLite write lock window: window_seconds=%.1f writes=%d held_seconds=%.2f "
                "short_held_seconds=%.2f max_held_seconds=%.2f waited_seconds=%.2f "
                "waited_writes=%d max_waited_seconds=%.2f top_scopes=[%s]",
                elapsed, self.writes, self.held, self.short_held, self.max_held,
                self.waited, self.waited_writes, self.max_waited, top,
            )
            self._started = now
            self._reset()


window = WriteLockWindow()
