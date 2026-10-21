"""Progress lines that keep desktop startup's log-churn signal current during a long phase."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

STARTUP_PROGRESS_INTERVAL_SECONDS = 30.0


@contextmanager
def startup_progress(logger, phase: str, interval: float = STARTUP_PROGRESS_INTERVAL_SECONDS):
    """Log that `phase` is still running every `interval` seconds until it ends."""
    stopped = threading.Event()
    began = time.monotonic()

    def report() -> None:
        while not stopped.wait(interval):
            logger.info(
                "startup phase: {} still running after {:.1f}s",
                phase,
                time.monotonic() - began,
            )

    reporter = threading.Thread(target=report, daemon=True)
    reporter.start()
    try:
        yield
    finally:
        stopped.set()
        reporter.join()
