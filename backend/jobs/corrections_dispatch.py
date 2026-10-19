"""Arrival queue dispatch for the corrections worker.

The worker owns mutable classifier state. This module owns only the generic FIFO
arrival loop so source hydration stays separate from model lifecycle state.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from loguru import logger

from infra.db_async import run_in_db
from jobs.corrections_recording import candidate_for_message, candidate_for_source


async def dispatch_arrival(worker, source_kind: str, source_id: str, enqueued_at: float) -> None:
    """Hydrate and classify one FIFO source, retaining an owed scan on failure."""
    started_at = worker.clock()
    worker.queue_delay_ms = (started_at - enqueued_at) * 1000
    try:
        candidate = await run_in_db(
            candidate_for_message if source_kind == "message" else candidate_for_source,
            source_id if source_kind == "message" else source_kind,
            *(() if source_kind == "message" else (source_id,)),
        )
        if candidate is not None:
            outcome = await worker.classify_arrival(replace(candidate, arrived=True))
            if outcome.kind in {"recorded", "unreadable"}:
                worker.release_hold()
    except Exception:
        logger.exception("corrections classifier arrival failed; the scan will read it")
        worker.owe_scan()
    finally:
        worker.service_time_ms = (worker.clock() - started_at) * 1000
        worker.queue.task_done()


def notify_from_thread(source_kind: str, source_id: str) -> None:
    """Hand an arrival to the worker on the application loop from a DB thread.

    The worker's queue, event and task are loop-bound, so a commit effect on a
    DB pool thread must not touch them. With no loop running (startup not yet
    reached or shutdown done) the arrival is owed to the scan instead, which the
    next startup runs.
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


async def run_dispatch(worker) -> None:
    """Consume arrivals until the queue is empty, then run the worker's scan pass."""
    while True:
        try:
            source_kind, source_id, enqueued_at = worker.queue.get_nowait()
        except asyncio.QueueEmpty:
            if worker.scan == "idle" or worker.held_reason is not None:
                return
            await worker.scan_pass()
            continue
        await dispatch_arrival(worker, source_kind, source_id, enqueued_at)
