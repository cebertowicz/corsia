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

from llm.local_setup import CAUSES

log = logging.getLogger(__name__)

EVENT = "onboarding.local_download"
CHIP = "Your local-model download changed."
SUMMARY = (
    "A local model download that was active when setup handed off has reached a terminal "
    "state. Tell the person the outcome and one next step; do not rebind the CORSIA agent."
)
OUTCOMES = frozenset({"ready", "failed", "start_failed", "setup_failed"})

_REF = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
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


# The flight owner supplies the stable failure cause.

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
        return "Quit your other oMLX, then retry."
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
    normalized_cause = str(cause) if cause in CAUSES else "other"
    detail["cause"] = normalized_cause
    detail["fix"] = _fix(normalized_cause, ref, bytes_to_free)
    if isinstance(bytes_to_free, int) and not isinstance(bytes_to_free, bool) and bytes_to_free > 0:
        detail["bytes_to_free"] = bytes_to_free
    return detail


def _notice(payload: dict[str, str | int]) -> str:
    return "## Local model download\n\n" + json.dumps(payload, sort_keys=True, ensure_ascii=False)


async def _deliver_once(conductor_id: str, delivery_id: str, payload: dict[str, Any]) -> bool:
    """Deliver a rendered wake once; a real failure remains durable and is logged once."""
    from events.delivery import deliver_to_thread
    from infra.db_async import run_in_db
    from infra import setup_progress

    try:
        delivered = await deliver_to_thread(
            conductor_id, EVENT, SUMMARY, delivery_id=delivery_id, chip=CHIP, notice=_notice(payload),
        )
    except Exception:  # noqa: BLE001 - a durable row permits a later render-path attempt
        log.error("Could not deliver local-download wake %s", delivery_id, exc_info=True)
        return False
    if delivered:
        await run_in_db(setup_progress.settle_local_download_wake, delivery_id)
    return delivered


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
            delivered = await _deliver_once(conductor_id, delivery_id, payload) and delivered
        return delivered


async def _pending() -> dict[str, dict]:
    from infra import setup_progress
    from infra.db_async import run_in_db

    return await run_in_db(setup_progress.pending_local_download_wakes)


async def _has_pending() -> bool:
    return bool(await _pending())



async def resume_pending_local_download() -> asyncio.Task[None] | None:
    """Resume durable delivery after backend startup; no outbox row means no live task."""
    from infra.corsia_agent import get_corsia_agent
    from infra.db_async import run_in_db

    conductor_id = await run_in_db(get_corsia_agent)
    if not conductor_id or not await _has_pending():
        return None
    await _flush_pending(conductor_id)
    return None


async def flush_pending_local_download(conductor_id: str | None = None) -> bool:
    """Flush the durable outbox after model selection has rendered the first-run wake."""
    from infra.corsia_agent import get_corsia_agent
    from infra.db_async import run_in_db

    current = await run_in_db(get_corsia_agent)
    if not current or (conductor_id is not None and conductor_id != current):
        return False
    return await _flush_pending(current)


async def notify_terminal_setup_download(
    flight_id: str,
    outcome: Literal["ready", "failed", "start_failed", "setup_failed"],
    *,
    model: str | None = None,
    cause: object = None,
    ref: str | None = None,
    bytes_to_free: int | None = None,
) -> bool:
    """Persist one terminal setup-flight outcome and flush it when first-run can receive it.

    ``ref`` is the local setup flight's safe reference; the agent-facing ``fix``
    is derived here. Returning false leaves the row durable for the render hook.
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
