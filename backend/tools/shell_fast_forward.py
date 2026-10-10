"""Cancellation cleanup for controlled repository fast-forwards."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from host_access.processes import PROCESS_OWNER

log = logging.getLogger(__name__)


async def settle_cancelled_fast_forward(
    work: Any,
    *,
    thread_id: str,
    turn_id: str,
    cancellation: asyncio.CancelledError,
) -> None:
    """Stop the owned process, observe its worker, then preserve cancellation."""

    def record_cleanup_failure(stage: str, error: BaseException) -> None:
        detail = (
            f"controlled fast-forward {stage} after cancellation "
            f"for thread {thread_id} turn {turn_id}: "
            f"{type(error).__name__}: {error}"
        )
        cancellation.add_note(detail)
        log.warning(detail)

    try:
        await PROCESS_OWNER.terminate_turn(
            thread_id,
            turn_id,
            foreground_only=True,
            reason="controlled_fast_forward_cancelled",
        )
    except BaseException as exc:
        record_cleanup_failure("process stop failed", exc)
    while not work.done():
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError as exc:
            record_cleanup_failure("worker wait was cancelled", exc)
            continue
        except BaseException:
            break
    try:
        work.result()
    except BaseException as exc:
        record_cleanup_failure("worker settlement failed", exc)
    raise cancellation
