"""Durably deliver terminal setup-download outcomes to the CORSIA agent.

The setup-progress record is the one outbox owner. A setup flight can finish
while the person is still on setup screens, so its outcome stays there until
the first-run wake is rendered and committed. Settings downloads never call
this module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Literal

log = logging.getLogger(__name__)

EVENT = "onboarding.local_download"
CHIP = "Your local-model download changed."
SUMMARY = (
    "A local model download that was active when setup handed off has reached a terminal "
    "state. Tell the person the outcome and one next step; do not rebind the CORSIA agent."
)
OUTCOMES = frozenset({"ready", "failed", "start_failed", "not_loaded"})
_RETRY_DELAYS_SECONDS = (0.1, 0.25, 0.5, 1.0)
_BACKGROUND_RETRY_DELAYS_SECONDS = (2.0, 5.0, 15.0)
_REF = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_retry_task: asyncio.Task[None] | None = None
_flush_lock = asyncio.Lock()


def _first_run_rendered(conductor_id: str) -> bool:
    """Whether setup's held first-run wake has been rendered and committed for this agent."""
    from infra.database import get_db
    from infra.onboarding_events import first_run_message_id, held_first_run_path

    db = get_db()
    try:
        exists = db.execute(
            "SELECT 1 FROM messages WHERE id=? AND thread_id=? LIMIT 1",
            (first_run_message_id(conductor_id), conductor_id),
        ).fetchone() is not None
        return exists and held_first_run_path(conductor_id, db=db) is None
    finally:
        db.close()


def _cause(value: object) -> str:
    """Map local exception names to the stable, agent-facing failure vocabulary."""
    if value in {"connection", "disk_full", "other", "memory", "other_omlx"}:
        return str(value)
    label = str(value or "").lower()
    if "disk" in label or "space" in label:
        return "disk_full"
    if "connect" in label or "network" in label:
        return "connection"
    if "memory" in label or "ram" in label:
        return "memory"
    if "foreign" in label or "omlx" in label:
        return "other_omlx"
    return "other"


def _fix(cause: str, ref: str | None, bytes_to_free: int | None) -> str:
    """Give the first-run agent one safe next step without leaking installer output."""
    if cause == "disk_full":
        amount = f"Free {bytes_to_free} bytes," if isinstance(bytes_to_free, int) and bytes_to_free > 0 else "Free space,"
        return amount + " then retry in Settings."
    if cause == "connection":
        return "Check the connection, then retry in Settings."
    if cause == "memory":
        return "Choose a subscription or API provider instead."
    if cause == "other_omlx":
        return "Check your oMLX in Settings, then retry the local model."
    if ref and _REF.fullmatch(ref):
        return f"Retry in Settings. Reference: {ref}."
    return "Retry in Settings."


def _payload(
    outcome: str, *, model: str | None, cause: object, ref: str | None, bytes_to_free: int | None,
) -> dict[str, str | int]:
    """Normalize the persisted and delivered event payload once."""
    detail: dict[str, str | int] = {"outcome": outcome}
    if outcome == "ready":
        if not model:
            raise ValueError("a ready local-download outcome needs its model")
        detail["model"] = model[:512]
        return detail
    normalized_cause = _cause(cause)
    detail["cause"] = normalized_cause
    detail["fix"] = _fix(normalized_cause, ref, bytes_to_free)
    if isinstance(bytes_to_free, int) and not isinstance(bytes_to_free, bool) and bytes_to_free > 0:
        detail["bytes_to_free"] = bytes_to_free
    return detail


def _notice(payload: dict[str, str | int]) -> str:
    return "## Local model download\n\n" + json.dumps(payload, sort_keys=True, ensure_ascii=False)


async def _deliver_with_retry(conductor_id: str, delivery_id: str, payload: dict[str, Any]) -> bool:
    """Try one outbox row a bounded number of times; leave it durable on every failure."""
    from events.delivery import deliver_to_thread
    from infra.db_async import run_in_db
    from infra import setup_progress

    for delay in (*_RETRY_DELAYS_SECONDS, None):
        try:
            delivered = await deliver_to_thread(
                conductor_id, EVENT, SUMMARY, delivery_id=delivery_id, chip=CHIP, notice=_notice(payload),
            )
            if delivered:
                await run_in_db(setup_progress.settle_local_download_wake, delivery_id)
                return True
        except Exception:  # noqa: BLE001 - the outbox preserves a safe retry path
            if delay is None:
                log.error("Could not deliver local-download wake %s", delivery_id, exc_info=True)
        if delay is None:
            log.error("Local-download wake delivery exhausted for %s", delivery_id)
            return False
        await asyncio.sleep(delay)
    return False


async def _flush_pending(conductor_id: str) -> bool:
    """Settle durable rows serially so a post-render flush cannot duplicate a retry."""
    from infra import setup_progress
    from infra.db_async import run_in_db

    async with _flush_lock:
        if not await run_in_db(_first_run_rendered, conductor_id):
            return False
        pending = await run_in_db(setup_progress.pending_local_download_wakes)
        if not pending:
            return True
        delivered = True
        for delivery_id, payload in pending.items():
            delivered = await _deliver_with_retry(conductor_id, delivery_id, payload) and delivered
        return delivered


async def _pending() -> dict[str, dict]:
    from infra import setup_progress
    from infra.db_async import run_in_db

    return await run_in_db(setup_progress.pending_local_download_wakes)


async def _has_pending() -> bool:
    return bool(await _pending())


async def _background_retry(conductor_id: str) -> None:
    """Retry a durable outbox row on a bounded schedule; the lifespan owns this task."""
    global _retry_task
    try:
        for delay in _BACKGROUND_RETRY_DELAYS_SECONDS:
            await asyncio.sleep(delay)
            if await _flush_pending(conductor_id):
                return
        pending = await _pending()
        for delivery_id in pending:
            log.error("Local-download wake retry exhausted; durable row remains: %s", delivery_id)
    finally:
        _retry_task = None


def retry_task() -> asyncio.Task[None] | None:
    """The exact retry owner that application lifespan starts and shuts down."""
    return _retry_task


def _schedule_retry(conductor_id: str) -> asyncio.Task[None]:
    """Start one lifecycle-owned retry task only while the durable outbox has work."""
    global _retry_task
    if _retry_task is None or _retry_task.done():
        _retry_task = asyncio.create_task(_background_retry(conductor_id))
    return _retry_task


async def resume_pending_local_download() -> asyncio.Task[None] | None:
    """Resume durable delivery after backend startup; no outbox row means no live task."""
    from infra.corsia_agent import get_corsia_agent
    from infra.db_async import run_in_db

    conductor_id = await run_in_db(get_corsia_agent)
    if not conductor_id or not await _has_pending():
        return None
    if await _flush_pending(conductor_id):
        return None
    return _schedule_retry(conductor_id)


async def flush_pending_local_download(conductor_id: str | None = None) -> bool:
    """Flush the durable outbox after model selection has rendered the first-run wake."""
    from infra.corsia_agent import get_corsia_agent
    from infra.db_async import run_in_db

    current = await run_in_db(get_corsia_agent)
    if not current or (conductor_id is not None and conductor_id != current):
        return False
    delivered = await _flush_pending(current)
    if not delivered and await _has_pending():
        _schedule_retry(current)
    return delivered


async def notify_terminal_setup_download(
    flight_id: str,
    outcome: Literal["ready", "failed", "start_failed", "not_loaded"],
    *,
    model: str | None = None,
    cause: object = None,
    ref: str | None = None,
    bytes_to_free: int | None = None,
) -> bool:
    """Persist one terminal setup-flight outcome and flush it when first-run can receive it.

    ``ref`` is the local setup flight's safe reference; the agent-facing ``fix``
    is derived here. Returning false means the row remains durable for the
    post-render flush and background retry, never that the terminal outcome was dropped.
    """
    if not flight_id:
        raise ValueError("flight_id is required")
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown local-download outcome: {outcome}")

    from infra import setup_progress
    from infra.db_async import run_in_db

    delivery_id = f"local-download:{flight_id}:{outcome}"
    await run_in_db(
        setup_progress.record_local_download_wake,
        delivery_id,
        _payload(outcome, model=model, cause=cause, ref=ref, bytes_to_free=bytes_to_free),
    )
    return await flush_pending_local_download()
