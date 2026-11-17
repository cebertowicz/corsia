"""Stop a read-only SQLite query once its wall-clock deadline passes.

SQLite calls the progress handler every ``DEADLINE_CHECK_STEPS`` VM steps. Each call is Python,
so it waits for the GIL; while other server threads hold it, a wait can last a whole switch
interval (5 ms). Checked every 1,000 steps (about 19,000 checks a second), a read's time grew
with how busy the server was rather than with its query, and reads timed out on a busy server.
At 1,000,000 steps a query is checked about every 50 ms, so it stops within a fraction of a
second of its deadline and a busy server slows it by a few percent.
"""

from __future__ import annotations

import sqlite3
import time

DEADLINE_CHECK_STEPS = 1_000_000


def stop_at(db: sqlite3.Connection, deadline: float) -> None:
    """Interrupt ``db``'s running statement once ``time.monotonic()`` reaches ``deadline``."""
    db.set_progress_handler(lambda: 1 if time.monotonic() >= deadline else 0, DEADLINE_CHECK_STEPS)
