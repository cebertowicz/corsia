"""Hand correction arrivals to the loop-bound worker from off-loop producers."""

from __future__ import annotations

from loguru import logger


def notify_from_thread(source_kind: str, source_id: str) -> None:
    """Notify the worker on the application loop from a DB pool thread's commit effect.

    The worker's queue, event and task are loop-bound, so a DB thread must not
    touch them. With no loop running (before startup or after shutdown) the
    arrival is owed to the scan instead.
    """
    from infra import app_loop
    from jobs import corrections_realtime as realtime

    async def _notify() -> None:
        realtime.notify(source_kind, source_id)

    try:
        app_loop.submit(_notify())
    except app_loop.AppLoopUnavailable:
        logger.info("no application loop for a {} arrival; the scan is owed", source_kind)
        realtime.WORKER.owe_scan()
