"""Restart recovery for CORSIA's one durable local-model setup flight."""

from __future__ import annotations

import asyncio
import logging

from llm import local_setup_operation
from llm.local_setup_gates import local_memory_ok

log = logging.getLogger(__name__)


async def resume_pending_setup() -> bool:
    """Re-follow a persisted oMLX task after restart without creating another."""
    from llm import local_setup as setup

    if setup._running():
        return True
    operation = local_setup_operation.read()
    if operation is None:
        return False
    if operation.kind not in {"setup", "starter", "settings"}:
        log.warning("discarding unreadable local setup recovery record")
        local_setup_operation.clear()
        return False
    if not await asyncio.to_thread(local_memory_ok):
        log.warning("discarding local setup recovery record: this Mac has too little memory for a local model")
        local_setup_operation.clear()
        return False
    model = operation.model

    setup._flight.result = None
    setup._flight.model = model
    setup._flight.kind = operation.kind
    setup._flight.stage = "Waiting to resume local model setup"

    async def resume() -> dict:
        if operation.kind == "settings":
            from llm.local_settings_download import resume as resume_settings_download

            return await resume_settings_download(model)
        await local_setup_operation.wait_for_lock(setup._flight)
        return await setup._run_flight(model=model)

    setup._flight.task = asyncio.create_task(resume())
    setup._flight.task.add_done_callback(setup._observe_setup_task)
    setup._flight.task.add_done_callback(lambda _task: setup._notify_models_changed())
    setup._emit("resumed", model=model.repository, kind=operation.kind)
    return True
