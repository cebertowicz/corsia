"""Cadence for the one continuation runner, including startup spread."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from automation.continuation import admission
from automation.continuation.runner import sweep

CONTINUATION_SWEEP_INTERVAL = 60
STARTUP_POLL_SECONDS = 1


async def continuation_sweep_loop() -> None:
    """Run startup-jittered sweeps, then the ordinary bounded cadence."""

    from loguru import logger

    started = datetime.now(timezone.utc)
    startup_until = started + timedelta(
        seconds=admission.RESTART_JITTER_SECONDS
    )
    first = True
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
        await asyncio.sleep(delay)
