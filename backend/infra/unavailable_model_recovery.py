"""Hold a FIFO head while its selected model is unavailable, and wake it to resume."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

from infra.db_async import run_in_db
from infra.send_target import (
    SELECTED_MODEL_UNAVAILABLE,
    TargetUnavailable,
    classify_target_admission,
)
from infra.send_types import DeliveryFailureSide

if TYPE_CHECKING:
    from infra.send_types import InboxQueuedInput
    from infra.thread_input_types import ClaimedBatch
    from llm.resolution import ExecutionTarget

_TASK: asyncio.Task[None] | None = None
_RERUN = False


def _retained_head_kind_sql(thread: str) -> str:
    """The actual eligible FIFO head's recoverable kind; trusted SQL argument only."""
    from connections.account_holds import ACCOUNT_HOLD_REASONS

    causes = ','.join("'" + cause + "'" for cause in ACCOUNT_HOLD_REASONS)
    return f"""(SELECT CASE
         WHEN held.delivery_error='{SELECTED_MODEL_UNAVAILABLE}' THEN 'model'
         WHEN held.delivery_error IN ({causes}) THEN 'account' END
      FROM messages AS held JOIN threads AS ht ON ht.id=held.thread_id
       WHERE held.thread_id={thread} AND held.delivery_state='queued'
         AND held.active_turn_id IS NULL AND held.write_outcome='proved_no_write'
         AND held.target_model=ht.model
         AND held.input_order=(
           SELECT MIN(head.input_order) FROM messages AS head
            WHERE head.thread_id=held.thread_id AND head.input_order IS NOT NULL
              AND head.delivery_state='queued'
              AND head.active_turn_id IS NULL))"""


def held_head_kind_sql(thread: str) -> str:
    """Shared recovery/Plans kind, independent of readiness and current binding."""
    return _retained_head_kind_sql(thread)


def held_head_sql(thread: str) -> str:
    """Recovery selects exactly the same retained set that Plans reads stalled."""
    return f"({held_head_kind_sql(thread)}) IS NOT NULL"


def _held_thread_ids() -> list[str]:
    """Read threads whose unclaimed FIFO head is held for model availability."""
    from infra.database import get_db

    db = get_db()
    try:
        return [
            str(row["thread_id"])
            for row in db.execute(
                f"SELECT t.id AS thread_id FROM threads AS t WHERE {held_head_sql('t.id')}",
            ).fetchall()
        ]
    finally:
        db.close()


async def hold_unavailable_model_input(
    batch: ClaimedBatch, target: ExecutionTarget,
) -> InboxQueuedInput:
    """Preserve a selected-model-unavailable batch at its FIFO boundary."""
    from core.plan_turn_end import record_turn_end_logged
    from core.turn_core import settle_turn_terminal
    from infra.input_writes import release_batch_before_write
    from infra.send_broadcast import broadcast_claimed_batch
    from infra.send_types import InboxQueuedInput, InputReceipt

    await run_in_db(
        release_batch_before_write, batch, hold_error=SELECTED_MODEL_UNAVAILABLE,
    )
    await settle_turn_terminal(batch.turn_id, "error", SELECTED_MODEL_UNAVAILABLE)
    try:
        await broadcast_claimed_batch(batch)
    except Exception:
        logger.exception("hold broadcast failed for thread {}", batch.thread_id)
    # The held Turn ended in error like any other, so plan liveness and a
    # delegating parent hear it (F1, #3317). No reset is passed: the held
    # head resumes through catalog recovery, never a second reset wake.
    await record_turn_end_logged(
        batch.thread_id, "plan-liveness after a held input failed",
        "parent drive after a held input failed", level="ERROR",
        outcome="error", error=_held_error(batch.thread_id, target),
        turn_id=batch.turn_id,
    )
    latest = batch.items[-1]
    receipt = InboxQueuedInput(InputReceipt(
        batch.thread_id, latest.message_id, latest.input_order,
        latest.revision + 1, latest.created_at,
    ))
    # A catalog notification that fired while this input was still claimed was
    # skipped; look once more now that the hold is committed and visible.
    schedule_unavailable_model_recovery()
    return receipt


async def hold_unless_runnable(
    target: ExecutionTarget, batch: ClaimedBatch,
) -> ExecutionTarget | InboxQueuedInput:
    """The admission seam: run a runnable target, hold the FIFO head otherwise.

    Callers resolve the target with ``send_target.resolve_target``, whose
    failures are ours and propagate to the caller's own failure settlement.
    Only the classifier's model-recoverable state is a wait; nothing outside
    admission asks.
    """
    admission = classify_target_admission(target)
    if admission == "run":
        return target
    if admission == "wait":
        return await hold_unavailable_model_input(batch, target)
    reason = target.availability_reason or "target_unavailable"
    raise TargetUnavailable(
        f"Selected target cannot run: {target.catalog_id} ({reason})",
        side=DeliveryFailureSide.PROVIDER,
    )


def _held_error(thread_id: str, target: ExecutionTarget) -> str:
    """Name the held model, why, and its known reset, for whoever reads the Turn end."""
    from infra.held_admission import limited_admission

    reason = target.availability_reason or "model_unavailable"
    if reason == "project_cloud_off":
        # The person's own setting, not an outage: name it so nobody reads it as one.
        from prompts.cloud_off import held_error

        return held_error(target.catalog_id)
    try:
        limit = limited_admission(thread_id, target.catalog_id)
    except Exception:
        logger.exception("limit read for held model {} failed", target.catalog_id)
        limit = None
    resets = limit.resets_at if limit is not None else None
    reset = f"; the provider reports a reset at {resets}" if resets else ""
    return f"Selected model unavailable: {target.catalog_id} ({reason}){reset}."


async def resolve_or_hold(
    model: str, batch: ClaimedBatch,
) -> ExecutionTarget | InboxQueuedInput:
    """Resolve ``model`` with the one resolver, then apply the admission seam."""
    from infra import send_target

    return await hold_unless_runnable(
        await send_target.resolve_target(model, thread_id=batch.thread_id), batch,
    )


async def _resume_held_heads() -> None:
    """Offer each held thread to the idle driver, whose claim decides release.

    The claim applies the same runnable predicate that held the input, so this
    only wakes the driver; one thread's failure never strands the others.
    """
    from infra.thread_execution import drive_idle_thread

    for thread_id in await run_in_db(_held_thread_ids):
        try:
            await drive_idle_thread(thread_id)
        except Exception:
            logger.exception("unavailable-model recovery failed for thread {}", thread_id)


async def _drain() -> None:
    """Coalesce concurrent catalog notifications into at most one rerun."""
    global _RERUN

    while True:
        _RERUN = False
        await _resume_held_heads()
        if not _RERUN:
            return


def _schedule_on_loop() -> None:
    global _RERUN, _TASK

    if _TASK is not None and not _TASK.done():
        _RERUN = True
        return
    from ws.ingress import track_background_task

    _TASK = asyncio.create_task(_drain())
    track_background_task(_TASK)


async def _schedule_from_submit() -> None:
    _schedule_on_loop()


def schedule_unavailable_model_recovery() -> None:
    """Schedule held-model recovery safely from an event loop or DB worker."""
    import infra.app_loop as app_loop

    try:
        app_loop.submit(_schedule_from_submit())
    except app_loop.AppLoopUnavailable:
        logger.warning(
            "unavailable-model recovery not scheduled: no live event loop; held "
            "inputs resume on the next catalog change or restart",
        )
