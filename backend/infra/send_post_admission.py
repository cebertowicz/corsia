"""Post-commit handling for a send whose input is already durably admitted."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable
from typing import Callable

from infra.db_async import run_in_db
from infra.send_types import InboxQueuedInput, SendRejected
from infra.thread_driver import InputAdmission
from llm.resolution import ExecutionTarget

log = logging.getLogger("infra.send_service")


async def target_for(
    admission: InputAdmission, supplied: ExecutionTarget | None,
) -> ExecutionTarget:
    from infra.send_target import resolve_target

    model = admission.accepted.target_model
    if supplied is not None and supplied.catalog_id == model:
        return supplied
    return await resolve_target(model)


async def publish_projection(step: str, admission: InputAdmission, call: Callable) -> None:
    """Run one display projection or timing wake; it never moves delivery.

    The input is durable and its delivery state is owned elsewhere, so a
    failure here is logged with its traceback and never fails the send.
    """
    try:
        result = call()
        if inspect.isawaitable(result):
            await result
    except Exception:
        log.exception(
            "post-admission %s failed for input %s",
            step, admission.accepted.message_id[:8],
        )


async def request_boundary_for_admission(
    admission: InputAdmission, supplied: ExecutionTarget | None,
) -> None:
    """Wake the active harness for an apply-now input at its safe boundary."""
    from infra import send_service

    target = await target_for(admission, supplied)
    await send_service.request_safe_boundary(admission.accepted.thread_id, target.harness)


async def fail_unrun_claim(admission: InputAdmission, error: BaseException) -> None:
    """Settle the claimed Turn no executor will run, then raise the sender's answer.

    Turn settlement is the owner that fails the input visibly with a reason
    (as ``drive_claimed_batch`` relies on). The sender then gets the same
    ``input_undeliverable`` answer a same-key retry gets. Always raises; a
    settlement failure propagates chained to the original error.
    """
    from core.turn_core import settle_turn_terminal, terminal_error_detail
    from infra.input_admission import input_delivery

    message_id = admission.accepted.message_id
    cancelled = isinstance(error, asyncio.CancelledError)
    try:
        await settle_turn_terminal(
            admission.batch.turn_id,  # type: ignore[union-attr]
            "cancelled" if cancelled else "error",
            terminal_error_detail(error) or None,
        )
    except Exception as settle_error:
        raise settle_error from error
    if not isinstance(error, Exception):
        raise error
    state, reason = await run_in_db(input_delivery, message_id)
    if state not in {"failed", "cancelled"}:
        raise error
    raise SendRejected(
        409,
        reason or "The accepted input failed delivery.",
        metadata={"cause": "input_delivery_failed", "message_ids": (message_id,)},
        input_undeliverable=True,
    ) from error


async def resolve_claimed_target(
    admission: InputAdmission,
    resolve: Callable[[], Awaitable[ExecutionTarget]],
) -> ExecutionTarget | InboxQueuedInput:
    """Resolve the claimed Turn's target, or settle the Turn no executor will run.

    An unrunnable target holds the FIFO head for recovery. Any failure before
    an executor owns the Turn, the hold's own included, settles it exactly as
    ``drive_claimed_batch`` does and raises the sender's answer.
    """
    from infra.unavailable_model_recovery import hold_unless_runnable

    try:
        return await hold_unless_runnable(await resolve(), admission.batch)
    except BaseException as error:
        await fail_unrun_claim(admission, error)
        raise


async def broadcast_passive(admission: InputAdmission, broadcast_input: bool) -> None:
    """Publish the passive context row that rode in with this admission."""
    if broadcast_input and admission.passive is not None:
        from infra.send_broadcast import broadcast_message_rows

        await publish_projection(
            "passive broadcast", admission,
            lambda: broadcast_message_rows(
                admission.accepted.thread_id, (admission.passive.message_id,),
            ),
        )
