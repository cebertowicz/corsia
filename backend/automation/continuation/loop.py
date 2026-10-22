"""Cadence for the one continuation runner, including startup spread."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from automation.continuation import admission
from automation.continuation.runner import sweep
import infra.sweep_wake as sweep_wake
from infra.db_async import run_in_db

CONTINUATION_SWEEP_INTERVAL = 60
STARTUP_POLL_SECONDS = 1


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
    nudged = sweep_wake.attach()
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
        # Row deadlines are durable: sleep until the next feedback batch is due, and wake early when a new one opens.
        # A row already overdue after the sweep failed to deliver, so it keeps the ordinary cadence.
        try:
            until_due = await run_in_db(_seconds_to_next_feedback_deadline, datetime.now(timezone.utc))
        except Exception as exc:  # noqa: BLE001
            logger.warning("continuation next-deadline read error: {}", exc)
            until_due = None
        if until_due is not None and 0 < until_due < delay:
            delay = until_due
        try:
            await asyncio.wait_for(nudged.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        nudged.clear()
