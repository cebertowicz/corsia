"""Deliver task-local lessons on native reads and persist their receipts.

This bridge owns the F4 read seam: the filesystem tool supplies an authorized
read result, while this module selects a task-local lesson and records one
unambiguous guidance receipt for every matched lesson.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from infra.db_async import run_in_db


@dataclass(frozen=True)
class LessonReadReceipt:
    """Task and per-lesson receipt data produced for one native skill read."""

    node_id: str | None = None
    receipts: tuple[Any, ...] = ()
    checkpoint_generation_id: str | None = None


async def apply_lesson_read_delivery(
    path: Path,
    *,
    rendered_text: str,
    data: bytes | None,
    thread_id: str | None,
) -> tuple[str, LessonReadReceipt]:
    """Apply a selected task's trial to a returned skill read, never to disk."""
    from core.skill_validation import is_skill_file

    empty = LessonReadReceipt()
    if data is None or not is_skill_file(path):
        return rendered_text, empty
    from infra import state as runtime_state
    from infra.database import get_db
    from loguru import logger

    checkpoint_generation_id = runtime_state._current_checkpoint_generation_id.get()

    def deliver():
        db = get_db()
        try:
            from core.lesson_delivery import read_delivery

            return read_delivery(
                db,
                thread_id=thread_id,
                path=path,
                rendered_text=rendered_text,
                checkpoint_generation_id=checkpoint_generation_id,
            )
        finally:
            db.close()

    try:
        delivered = await run_in_db(deliver)
    except Exception:  # noqa: BLE001 - a read remains usable if delivery is unavailable
        logger.exception("lesson_delivery: unable to prepare trial delivery for %s", path)
        return rendered_text, empty
    return delivered.text, LessonReadReceipt(
        node_id=delivered.node_id,
        receipts=tuple(delivered.receipts),
        checkpoint_generation_id=checkpoint_generation_id,
    )


async def record_lesson_read_receipts(
    path: Path,
    *,
    rendered_text: str,
    data: bytes | None,
    whole: bool,
    thread_id: str | None,
    receipt: LessonReadReceipt,
) -> None:
    """Persist one version receipt for every lesson matched by an on-demand read."""
    from core.skill_validation import is_skill_file

    if data is None or not is_skill_file(path):
        return
    from core.guidance_manifest import record_guidance_load
    from infra import state as runtime_state

    identity = runtime_state._current_turn_identity.get(None)
    receipt_rows = receipt.receipts or (None,)
    for trial_receipt in receipt_rows:
        await asyncio.to_thread(
            record_guidance_load,
            thread_id=thread_id,
            turn_id=getattr(identity, "turn_id", None),
            path=path,
            data=data,
            returned_text=rendered_text,
            partial=not whole,
            node_id=receipt.node_id,
            variant=getattr(trial_receipt, "variant", None),
            checkpoint_generation_id=receipt.checkpoint_generation_id,
            lesson_id=getattr(trial_receipt, "lesson_id", None),
            lesson_behavioral_sha256=getattr(trial_receipt, "behavioral_sha256", None),
        )
