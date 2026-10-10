"""The one writer thread: drain the capture queue into the store.

Batches are at most 24 rows and 384 KiB per insert transaction. Age pruning
runs every ten minutes; size pruning runs inside the store's pre-write check.
A store error drops and counts that batch and writes one stderr line; after
five in a row the writer backs off, up to 60 s. Nothing here raises into the
process or logs through a captured logger.
"""

from __future__ import annotations

import threading
import time

from infra.otel_logs.capture import CaptureQueue, LogCapture
from infra.otel_logs.store import LogStore, stderr_line

BATCH_ROWS = 24
BATCH_BYTES = 384 * 1024
WAIT_SECONDS = 1.0
PRUNE_INTERVAL_SECONDS = 600.0
MAX_BACKOFF_SECONDS = 60.0
SHUTDOWN_SECONDS = 5.0


class LogWriter:
    """Own the store connection on one daemon thread."""

    def __init__(self, store: LogStore, queue: CaptureQueue, capture: LogCapture) -> None:
        self.store = store
        self.queue = queue
        self.capture = capture
        self.dropped_shutdown = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failures = 0
        self._next_prune = 0.0

    def start(self) -> None:
        """Start the daemon writer thread."""
        self._thread = threading.Thread(
            target=self._run, name="otel-logs-writer", daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        self.capture.exclude_thread(threading.get_ident())
        self._next_prune = time.monotonic() + PRUNE_INTERVAL_SECONDS
        while not self._stop.is_set():
            self.queue.wait(WAIT_SECONDS)
            self._drain(deadline=None)
            if time.monotonic() >= self._next_prune:
                self._next_prune = time.monotonic() + PRUNE_INTERVAL_SECONDS
                self._guarded(lambda: self.store.prune(time.time_ns()), 0)

    def _drain(self, deadline: float | None) -> None:
        while not self._stop.is_set() or deadline is not None:
            if deadline is not None and time.monotonic() >= deadline:
                return
            batch = self.queue.drain(BATCH_ROWS, BATCH_BYTES)
            if not batch:
                return
            self._guarded(lambda: self.store.insert(batch, time.time_ns()), len(batch))

    def _guarded(self, action, batch_rows: int) -> None:
        try:
            action()
            self._failures = 0
        except Exception as exc:
            self.store.counters.dropped_store_error += batch_rows
            self._failures += 1
            stderr_line(f"store error ({type(exc).__name__}): {exc}")
            if self._failures >= 5:
                self._stop.wait(min(MAX_BACKOFF_SECONDS, 2.0 ** (self._failures - 4)))

    def stop(self, timeout: float = SHUTDOWN_SECONDS) -> None:
        """Stop the loop, commit queued rows for at most ``timeout``, count the rest."""
        self._stop.set()
        self.queue.wake()
        deadline = time.monotonic() + timeout
        if self._thread is not None:
            self._thread.join(max(0.0, deadline - time.monotonic()))
            if not self._thread.is_alive():
                self._drain(deadline=deadline)
        self.dropped_shutdown += len(self.queue.drain(len(self.queue) or 1, 1 << 62))
        if self._thread is None or not self._thread.is_alive():
            self.store.close()
