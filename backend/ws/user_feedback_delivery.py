"""Debounced delivery of a person's live-document feedback."""

from __future__ import annotations

import asyncio

from infra.db_async import run_in_db

USER_FEEDBACK_IDLE_SECONDS = 5
_generations: dict[str, int] = {}
_tasks: dict[str, asyncio.Task] = {}


def _close(artifact_id: str) -> list[str]:
    """Close one person's persisted feedback after typing goes quiet."""
    from doceditor import feedback_batches
    from infra.database import get_db

    db = get_db()
    try:
        return feedback_batches.close_for_document(
            db,
            author_kind="user",
            author_id=feedback_batches.USER_AUTHOR_ID,
            artifact_id=artifact_id,
            reason="idle",
        )
    finally:
        db.close()


async def deliver(artifact_id: str) -> None:
    """Drive recipients after the feedback close has committed."""
    woken = await run_in_db(_close, artifact_id)
    if woken:
        from plans import public

        await public.drive_threads(woken)


def cancel(artifact_id: str) -> None:
    task = _tasks.pop(artifact_id, None)
    if task is not None:
        task.cancel()


def schedule(artifact_id: str) -> None:
    """Deliver persisted user edits five seconds after the last accepted update."""
    generation = _generations.get(artifact_id, 0) + 1
    _generations[artifact_id] = generation
    cancel(artifact_id)

    async def send() -> None:
        try:
            await asyncio.sleep(USER_FEEDBACK_IDLE_SECONDS)
            if _generations.get(artifact_id) == generation:
                await deliver(artifact_id)
        finally:
            if _tasks.get(artifact_id) is task:
                _tasks.pop(artifact_id, None)

    task = asyncio.create_task(send())
    _tasks[artifact_id] = task
