"""One shared admission and delivery entry point for every thread sender."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable
from functools import wraps
from typing import Any, Callable

from infra.db_async import run_in_db
from infra.send_broadcast import broadcast_input_row, broadcast_newest_error
from infra.send_post_admission import (
    broadcast_passive,
    publish_projection,
    request_boundary_for_admission,
    resolve_claimed_target,
    target_for as _target_for,
)
from infra.send_types import InboxQueuedInput, InputReceipt, SendRejected, SendResult
from infra.input_preparation import admit_prepared_input
from infra.thread_driver import InputAdmission
from infra.thread_input_types import HeldAdmission
from infra.time import utc_now_iso
from llm.resolution import ExecutionTarget, resolve_execution_target


def _drain_admitted(function):
    """Count ingress until its durable admission decision has committed."""

    @wraps(function)
    async def guarded(*args, **kwargs):
        from infra.drain import (
            begin_turn_admission, finish_turn_admission, turn_admission_is_inherited,
        )

        if turn_admission_is_inherited():
            return await function(*args, **kwargs)
        admission = begin_turn_admission()
        try:
            return await function(*args, **kwargs)
        finally:
            finish_turn_admission(admission)

    return guarded


async def _bind_image_identity(
    thread_id: str,
    thread_row: Any,
    attachments: list | None,
    target: ExecutionTarget | None,
) -> list:
    """Validate fresh uploads and persist their exact admitted byte identity."""
    if not attachments:
        return []
    from core.media_delivery import (
        bind_projection_to_descriptor, build_delivery_projection_async,
    )
    from core.media_projection import MediaProjectionError

    thread_row_dict = dict(thread_row)
    model = str(thread_row_dict.get("model") or "").strip()
    # No model: validate and bind the source only; delivery fits the chosen model.
    resolved = target or (await run_in_db(resolve_execution_target, model) if model else None)
    try:
        public, projection = await build_delivery_projection_async(
            attachments,
            owner_id=str(thread_row_dict.get("user_id") or ""),
            thread_id=thread_id,
            target=resolved,
        )
    except MediaProjectionError as error:
        invalid_destination = error.cause.startswith(("image_model", "image_harness"))
        raise SendRejected(
            409 if invalid_destination else 422,
            error.detail,
            metadata={"cause": error.cause, "legal_action": error.legal_action},
        ) from error
    return bind_projection_to_descriptor(public, projection)


def _delivery_options(
    *,
    native_mode: str,
    attachments: list | None,
    reasoning_effort: str | None,
    accepted_effects: dict | None,
    broadcast_input: bool,
    touch_thread_on_insert: bool | None,
) -> dict:
    return {
        "native_mode": native_mode,
        "attachments": attachments or [],
        "reasoning_effort": reasoning_effort,
        "accepted_effects": accepted_effects,
        "broadcast_input": broadcast_input,
        "touch_session_on_insert": touch_thread_on_insert,
    }


def _receipt(admission: InputAdmission) -> InputReceipt:
    accepted = admission.accepted
    return InputReceipt(
        accepted.thread_id,
        accepted.message_id,
        accepted.input_order,
        accepted.revision,
        accepted.created_at,
    )


def _require_thread_identity(thread_id: str, thread_row: Any) -> None:
    try:
        supplied_id = thread_row["id"]
    except (KeyError, TypeError):
        supplied_id = None
    if str(supplied_id or "") != thread_id:
        raise SendRejected(404, "thread does not exist")


async def broadcast_accepted_input(
    admission: InputAdmission,
    content: str,
    metadata: dict | None,
    *,
    role: str,
    exclude_ws: Any | None,
) -> None:
    """Project the exact durable pending row, any hold it took, and the error it now waits behind."""
    accepted = admission.accepted
    broadcast_input_row(
        accepted.thread_id, accepted.message_id, content, metadata, accepted.created_at,
        delivery_state="queued",
        delivery_error=accepted.delivery_error,
        write_outcome="proved_no_write" if accepted.delivery_error else None,
        delivery_revision=accepted.revision,
        role=role,
        exclude_ws=exclude_ws,
    )
    await broadcast_newest_error(accepted.thread_id)


async def request_safe_boundary(thread_id: str, target_harness: str) -> None:
    """Tell the active harness that an apply-now FIFO prefix is available."""
    if target_harness == "codex":
        from subagents.codex_runtime_event_delivery import schedule_safe_boundary

        schedule_safe_boundary(thread_id)
    # Native checks durable FIFO at every provider-safe boundary already.
    # Claude Code writes only while a tool reply is held (#2204).


async def execute_claimed_batch(
    batch, **kwargs,
) -> SendResult | InboxQueuedInput:
    """Load the provider executor lazily to keep admission dependency-light."""
    from infra.thread_execution import execute_claimed_batch as execute

    return await execute(batch, **kwargs)


async def _raise_if_committed_failure(admission: InputAdmission) -> str | None:
    """Publish a failed prefix and report failure only for this accepted input."""
    from infra.thread_input_types import FailedInputBatch

    if isinstance(admission.batch, FailedInputBatch):
        from infra.turn_admission_driver import drive_turn_admission

        await drive_turn_admission(admission.batch)
        if admission.accepted.message_id in admission.batch.message_ids:
            raise SendRejected(
                409,
                admission.batch.error_text,
                metadata={
                    "cause": "plan_admission_failed",
                    "message_ids": admission.batch.message_ids,
                },
                input_undeliverable=True,
            )
        from infra.input_admission import input_delivery

        state, error = await run_in_db(
            input_delivery, admission.accepted.message_id,
        )
        if state in {"failed", "cancelled"}:
            raise SendRejected(
                409,
                error or "The accepted input did not reach a Turn.",
                metadata={
                    "cause": "input_delivery_failed",
                    "message_ids": (admission.accepted.message_id,),
                },
                input_undeliverable=True,
            )
        return state
    if (
        admission.accepted.duplicate
        and admission.accepted.delivery_state in {"failed", "cancelled"}
    ):
        raise SendRejected(
            409,
            admission.accepted.delivery_error or "The accepted input failed delivery.",
            metadata={
                "cause": "input_delivery_failed",
                "message_ids": (admission.accepted.message_id,),
            },
            input_undeliverable=True,
        )
    return None


async def _deliver_admission(
    admission: InputAdmission,
    thread_row: Any,
    *,
    delivery_intent: str,
    target: ExecutionTarget | None,
    user: dict | None,
    ws: Any | None,
    dispatch_content: str | None,
    after_persist_before_dispatch: Callable | None,
    text: str,
    metadata: dict | None,
    role: str,
    broadcast_input: bool,
    broadcast_exclude_ws: Any | None,
    on_admitted: Callable[[InboxQueuedInput], Awaitable[None]] | None,
) -> SendResult | InboxQueuedInput:
    delivery_after_failed_prefix = await _raise_if_committed_failure(admission)
    await broadcast_passive(admission, broadcast_input)
    held = admission.accepted.delivery_error is not None
    limited = admission.batch if isinstance(admission.batch, HeldAdmission) else None
    if (
        broadcast_input
        and not admission.accepted.duplicate
        and delivery_after_failed_prefix in (None, "queued")
    ):
        await publish_projection(
            "input broadcast", admission,
            lambda: broadcast_accepted_input(
                admission, text, metadata, role=role,
                exclude_ws=None if held else broadcast_exclude_ws,
            ),
        )
    queued = InboxQueuedInput(
        _receipt(admission),
        duplicate=admission.accepted.duplicate,
        held=limited,
        admission_hold=admission.accepted.delivery_error if held else None,
    )
    if on_admitted is not None:
        await publish_projection("on_admitted", admission, lambda: on_admitted(queued))
    if limited is not None:
        from infra.turn_admission_driver import drive_turn_admission

        await drive_turn_admission(limited)
        return queued
    if admission.accepted.duplicate or held:
        return queued
    if admission.held_for_restart:
        return queued
    if delivery_after_failed_prefix is not None:
        return queued
    if admission.batch is None:
        from thread.model_pending import drive_pending_model_switch

        thread_id = admission.accepted.thread_id
        pending_switch = await drive_pending_model_switch(thread_id)
        if pending_switch is not None:
            return queued
        if delivery_intent == "apply_now":
            # A timing wake only: the active Turn's settlement drains the FIFO.
            await publish_projection(
                "safe boundary", admission,
                lambda: request_boundary_for_admission(admission, target),
            )
        return queued
    resolved = await resolve_claimed_target(
        admission, lambda: _target_for(admission, target),
    )
    if isinstance(resolved, InboxQueuedInput):
        return resolved
    return await execute_claimed_batch(
        admission.batch,
        thread_row=dict(thread_row),
        target=resolved,
        user=user,
        ws=ws,
        dispatch_content=dispatch_content,
        after_persist_before_dispatch=after_persist_before_dispatch,
    )


@_drain_admitted
async def send_thread_input(
    thread_id: str,
    thread_row: Any,
    content: str,
    *,
    role: str = "user",
    user: dict | None = None,
    metadata: dict | None = None,
    delivery_intent: str = "apply_now",
    idempotency_key: str | None = None,
    input_id: str | None = None,
    native_mode: str,
    dispatch_content: str | None = None,
    persist_input: bool = True,
    broadcast_input: bool = True,
    touch_thread_on_insert: bool | None = None,
    trim_content: bool = True,
    allow_empty_content: bool = False,
    ws: Any | None = None,
    requested_model_id: str | None = None,
    target: ExecutionTarget | None = None,
    attachments: list | None = None,
    is_retry: bool = False,
    reasoning_effort: str | None = None,
    after_persist_before_dispatch: Callable | None = None,
    broadcast_exclude_ws: Any | None = None,
    accepted_effects: dict | None = None,
    on_admitted: Callable[[InboxQueuedInput], Awaitable[None]] | None = None,
    continuation: dict | None = None,
    **_removed_internal_options,
) -> SendResult | InboxQueuedInput:
    """Accept one input exactly once, then deliver or request a safe boundary.

    ``on_admitted`` runs after the durable broadcast and before delivery work.
    """
    del persist_input, is_retry, requested_model_id
    _require_thread_identity(thread_id, thread_row)
    if native_mode == "ws" and ws is None:
        raise SendRejected(500, "Native WebSocket send missing socket context")
    from infra.reply_context import RejectedComments, has_comment_candidates
    comment_candidates = has_comment_candidates((metadata or {}).get("reply_context"))
    text = _accepted_text(content, trim_content, allow_empty_content, attachments, comment_candidates)
    message_id = input_id or idempotency_key or str(uuid.uuid4())
    admitted_at = utc_now_iso()
    descriptor_attachments = await _bind_image_identity(
        thread_id, thread_row, attachments, target,
    )
    from infra.send_effects import validate_accepted_effects
    options = _delivery_options(
        native_mode=native_mode,
        attachments=descriptor_attachments,
        reasoning_effort=reasoning_effort,
        accepted_effects=validate_accepted_effects(accepted_effects),
        broadcast_input=broadcast_input,
        touch_thread_on_insert=touch_thread_on_insert,
    )
    try:
        admission = await admit_prepared_input(
            thread_id, role=role, content=text, intent=delivery_intent,
            message_id=message_id, turn_id=str(uuid.uuid4()), boot_epoch=_boot_epoch(),
            admitted_at=admitted_at, metadata=metadata, delivery_options=options,
            continuation=continuation,
        )
    except RejectedComments as exc:
        raise SendRejected(400, str(exc)) from exc
    from jobs.corrections_recording import notify_if_owner  # best effort; never fails the send
    notify_if_owner(admission.accepted, role=role, content=text, metadata=metadata)
    return await _deliver_admission(
        admission, thread_row,
        delivery_intent=delivery_intent,
        target=target, user=user, ws=ws,
        dispatch_content=dispatch_content,
        after_persist_before_dispatch=after_persist_before_dispatch,
        text=text, metadata=metadata, role=role,
        broadcast_input=broadcast_input,
        broadcast_exclude_ws=broadcast_exclude_ws,
        on_admitted=on_admitted,
    )


def _accepted_text(
    content: str,
    trim_content: bool,
    allow_empty_content: bool,
    attachments: Any | None,
    comment_candidates: bool = False,
) -> str:
    """Normalize the input text and refuse one that carries nothing.

    Raises:
        SendRejected: The input has neither content nor permitted attachments.
    """
    text = content.strip() if trim_content else content
    if not text and not (allow_empty_content and attachments) and not comment_candidates:
        raise SendRejected(400, "Message content is empty")
    return text


def _boot_epoch() -> str:
    from core.turn_core import BOOT_EPOCH

    return BOOT_EPOCH


__all__ = ["InboxQueuedInput", "SendRejected", "SendResult", "send_thread_input"]
