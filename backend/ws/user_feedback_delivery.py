"""Schedule durable user-feedback delivery from the live editor's idle boundary."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from infra.db_async import run_in_db

log = logging.getLogger(__name__)
_tasks: dict[str, asyncio.Task[None]] = {}


def _close(artifact_id: str) -> list[str]:
    """Sweep one document through its persisted feedback deadline."""
    from doceditor import feedback_batches
    from infra.database import get_db

    db = get_db()
    try:
        return feedback_batches.close_due(
            datetime.now(timezone.utc).isoformat(),
            db=db,
            artifact_id=artifact_id,
        )
    finally:
        db.close()


async def deliver(artifact_id: str) -> None:
    """Drive recipients after the persisted due batches have committed."""
    woken = await run_in_db(_close, artifact_id)
    if woken:
        from plans import public

        await public.drive_threads(woken)


def cancel(artifact_id: str) -> None:
    """Cancel only a pending idle wait for one document."""
    task = _tasks.pop(artifact_id, None)
    if task is not None:
        task.cancel()


def _report_delivery_failure(task: asyncio.Task[None]) -> None:
    """Consume and report a detached delivery failure without hiding it."""
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        log.exception("live document feedback delivery failed", exc_info=True)


def schedule(artifact_id: str) -> None:
    """Sweep the document five seconds after its last accepted live-editor update."""
    from doceditor.feedback_batches import USER_IDLE

    cancel(artifact_id)

    async def send() -> None:
        try:
            await asyncio.sleep(USER_IDLE)
        except asyncio.CancelledError:
            return
        if _tasks.get(artifact_id) is task:
            _tasks.pop(artifact_id, None)
        await deliver(artifact_id)

    task = asyncio.create_task(send())
    task.add_done_callback(_report_delivery_failure)
    _tasks[artifact_id] = task
