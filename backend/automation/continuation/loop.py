"""Cadence for the one continuation runner, including startup spread."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from automation.continuation import restart_jitter
from automation.continuation.runner import sweep
import infra.sweep_wake as sweep_wake
from infra.db_async import run_in_db

CONTINUATION_SWEEP_INTERVAL = 60
STARTUP_POLL_SECONDS = 1


def _seconds_to_next_feedback_deadline(now: datetime) -> float | None:
    from automation.continuation.sources.feedback_batches import FeedbackBatchSource
    from infra.database import get_db

    db = get_db()
    try:
        return FeedbackBatchSource().seconds_until_next_deadline(now, db=db)
    finally:
        db.close()


async def continuation_sweep_loop() -> None:
    """Run startup-jittered sweeps, then the ordinary bounded cadence."""

    from loguru import logger

    started = datetime.now(timezone.utc)
    startup_until = started + timedelta(
        seconds=restart_jitter.RESTART_JITTER_SECONDS
    )
    first = True
    nudged = sweep_wake.attach()
    while True:
        now = datetime.now(timezone.utc)
        try:
            await sweep(
                now.isoformat(),
                startup_jitter_seconds=(
                    restart_jitter.RESTART_JITTER_SECONDS if first else 0
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
        # Row deadlines are durable: sleep until the next feedback batch is due, and wake early when a new one opens.
        # Read from the instant the sweep checked: a row due by then failed to deliver and keeps the ordinary cadence,
        # while one that came due as the sweep ran was never tried, so it goes at once.
        try:
            until_due = await run_in_db(_seconds_to_next_feedback_deadline, now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("continuation next-deadline read error: {}", exc)
            until_due = None
        if until_due is not None:
            elapsed = (datetime.now(timezone.utc) - now).total_seconds()
            delay = min(delay, max(0.0, until_due - elapsed))
        # asyncio.timeout waits inside this task; on 3.11 wait_for would start and drop a task every poll.
        try:
            async with asyncio.timeout(delay):
                await nudged.wait()
        except TimeoutError:
            pass
        nudged.clear()
