"""Cadence for the one continuation runner, including startup spread."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from automation.continuation import admission, wake
from automation.continuation.runner import sweep
from infra.db_async import run_in_db

CONTINUATION_SWEEP_INTERVAL = 60
STARTUP_POLL_SECONDS = 1
# A due row that a sweep could not deliver is retried this often, never in a tight loop.
DUE_RETRY_SECONDS = 5


def _seconds_to_next_feedback_deadline(now: datetime) -> float | None:
    from doceditor import feedback_state
    from infra.database import get_db

    db = get_db()
    try:
        return feedback_state.seconds_until_next_due(now, db=db)
    finally:
        db.close()


async def continuation_sweep_loop() -> None:
    """Run startup-jittered sweeps, then the ordinary bounded cadence."""

    from loguru import logger

    started = datetime.now(timezone.utc)
    startup_until = started + timedelta(
        seconds=admission.RESTART_JITTER_SECONDS
    )
    first = True
    nudged = wake.attach()
    while True:
        now = datetime.now(timezone.utc)
        try:
            await sweep(
                now.isoformat(),
                startup_jitter_seconds=(
                    admission.RESTART_JITTER_SECONDS if first else 0
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("continuation sweep error: {}", exc)
        first = False
        delay = (
            STARTUP_POLL_SECONDS
            if now < startup_until
            else CONTINUATION_SWEEP_INTERVAL
        )
        # Row deadlines are durable: sleep until the next feedback batch is due, and wake early when one is written.
        until_due = await run_in_db(_seconds_to_next_feedback_deadline, datetime.now(timezone.utc))
        if until_due is not None and until_due < delay:
            delay = max(until_due, DUE_RETRY_SECONDS if until_due <= 0 else 0)
        try:
            await asyncio.wait_for(nudged.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        nudged.clear()
