"""Compensation for acquired Settings restart ownership."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable


def failure_detail(failure: BaseException) -> str | None:
    """Return durable update-log detail when a preparation failure wrote one."""
    log_path = getattr(failure, "update_log_path", None)
    return json.dumps({"log_path": str(log_path)}) if log_path else None


async def compensate_cancelled_acquire(
    acquired_ids: list[str],
    cancellation: asyncio.CancelledError,
) -> None:
    """Release committed ownership without replacing caller cancellation."""
    if not acquired_ids:
        return
    from automation.restart import abort_restart_control

    try:
        await abort_restart_control(
            acquired_ids[0],
            error="settings restart cancelled before launch",
            cancelled=True,
            failure_code="drain_cancelled",
        )
    except Exception as error:
        cancellation.add_note(f"Restart cancellation compensation failed: {error}")
    except asyncio.CancelledError:
        cancellation.add_note("Restart cancellation compensation was interrupted.")


async def compensate_preparation_failure(
    operation_id: str,
    failure: BaseException,
    *,
    failure_code: str | None = None,
    failure_detail: str | None = None,
) -> None:
    """Release the exact reservation without replacing its preparation failure."""
    from automation.restart import abort_restart_control

    try:
        released = await abort_restart_control(
            operation_id,
            error=str(getattr(failure, "detail", failure))[:500],
            failure_code=failure_code,
            failure_detail=failure_detail,
        )
    except asyncio.CancelledError:
        failure.add_note("Restart preparation compensation was interrupted.")
    except Exception as error:
        failure.add_note(f"Restart preparation compensation failed: {error}")
    else:
        if not released:
            failure.add_note("Restart preparation compensation lost exact ownership.")


async def claim_scheduled_choice(
    operation_id: str,
    acquired_ids: list[str],
    claim_hook: Callable[[], bool],
) -> bool:
    """Claim Tonight only after the durable operation exists or compensate it."""
    from infra.db_async import run_in_db

    try:
        claimed = await run_in_db(claim_hook)
    except asyncio.CancelledError as error:
        await compensate_cancelled_acquire(acquired_ids, error)
        raise
    except BaseException as error:
        await compensate_preparation_failure(operation_id, error)
        raise
    if not claimed:
        from automation.restart import abort_restart_control

        await abort_restart_control(
            operation_id,
            error="scheduled update choice changed before launch",
            cancelled=True,
        )
    return claimed
