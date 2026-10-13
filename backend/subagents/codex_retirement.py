"""Exact-owner retirement for a committed model switch."""

from __future__ import annotations

import logging


log = logging.getLogger(__name__)


async def retire_if_current(
    thread_id: str,
    *,
    expected_owner,
    db=None,
    broadcast=None,
    operation_id: str | None = None,
) -> bool:
    """Retire only the Codex owner captured before a context commit."""
    from subagents import codex_runtime

    lock = codex_runtime._LIVE.lock(thread_id)
    async with lock:
        current_owner = codex_runtime.current_handle_fingerprint(thread_id)
        if current_owner is None:
            if expected_owner is not None:
                log.info(
                    "model switch source runtime branch=owner_gone thread_id=%s operation_id=%s",
                    thread_id, operation_id or "unknown",
                )
            # No live handle, but the durable provider thread must not resume.
            await codex_runtime._retire_locked(thread_id, db=db, broadcast=broadcast)
            return True
        if expected_owner is None or current_owner != expected_owner:
            log.warning(
                "model switch source runtime branch=owner_changed thread_id=%s operation_id=%s",
                thread_id, operation_id or "unknown",
            )
            return False
        retired = await codex_runtime._retire_locked(
            thread_id,
            db=db,
            broadcast=broadcast,
        )
        if not retired:
            log.warning(
                "model switch source runtime branch=retire_failed thread_id=%s operation_id=%s",
                thread_id, operation_id or "unknown",
            )
        return retired
