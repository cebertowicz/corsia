"""Cancellation for the canonical local setup flight."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from llm import local_setup_operation
from llm.providers.omlx_endpoints import admin_request


async def cancel_setup(flight: Any, *, notify: Callable[[], None], emit: Callable[..., None]) -> dict:
    """Cancel the live oMLX download; a completion race is deliberately a no-op."""
    task = flight.task
    if task is None or task.done():
        return {"ok": True, "cancelled": False}

    operation = local_setup_operation.read()
    task_id = operation.task_id if operation else None
    if task_id and flight.server_id:
        response = await admin_request(
            {"server_id": flight.server_id}, "POST", f"/hf/cancel/{task_id}", timeout=15.0,
        )
        # oMLX reports a just-finished task as absent or conflicting. It is already
        # in the requested terminal state, so do not turn that race into an error.
        if response.status_code not in (200, 202, 204, 404, 409):
            raise RuntimeError(f"oMLX could not cancel the model download (HTTP {response.status_code}).")

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    flight.result = {"ok": True, "cancelled": True}
    flight.stage = None
    flight.following = False
    flight.download_eta_seconds = None
    flight.download_stalled = False
    local_setup_operation.clear()
    notify()
    model = operation.repository if operation else getattr(flight.model, "repository", None)
    emit("cancelled", model=model)
    return {"ok": True, "cancelled": True}
