"""Provider-runtime transition and notifications after a model commit."""

from __future__ import annotations

import logging
from typing import Any

from infra.product_events import emit_product_event
from infra.time import utc_now_iso

log = logging.getLogger(__name__)


def _broadcast_model_selection(
    thread_id: str,
    *,
    new_model: str,
    user_id: str | None,
    committed_at: str | None,
    account_update: dict | None = None,
) -> None:
    from infra import state

    update = {
        "type": "session_updated",
        "thread_id": thread_id,
        "model": new_model,
        "pending_model": None,
        "switch_status": None,
        "switch_error": None,
        "switch_operation_id": None,
        "updated_at": committed_at or utc_now_iso(),
    }
    if account_update is not None:
        update.update(account_update)
    if user_id:
        state.broadcast_event(thread_id, user_id, update)
    else:
        state.broadcast_to_thread(thread_id, update)


def publish_first_model_selection(
    thread_id: str,
    *,
    new_model: str,
    user_id: str | None,
    committed_at: str,
) -> None:
    """Publish a thread's first model; no source runtime exists to transition."""
    _broadcast_model_selection(
        thread_id, new_model=new_model, user_id=user_id, committed_at=committed_at,
    )
    emit_product_event(
        "model", "switched", thread_id=thread_id, user_id=user_id,
        properties={"model_id": new_model},
    )


def broadcast_pending_switch(request, *, error: str | None = None) -> None:
    """Publish the durable pending or failed selection without changing model."""
    from infra import state

    state.broadcast_to_thread(request.thread_id, {
        "type": "session_updated",
        "thread_id": request.thread_id,
        "pending_model": request.destination_model,
        "switch_status": "failed" if error is not None else request.status,
        "switch_error": error,
        "switch_operation_id": request.operation_id,
        "updated_at": request.requested_at,
    })


async def broadcast_switch_inputs(request) -> None:
    """Publish changed delivery revisions through the canonical input projection."""
    from infra.send_broadcast import broadcast_message_rows

    if request is not None and request.updated_input_ids:
        await broadcast_message_rows(request.thread_id, request.updated_input_ids)


async def _transition_claude_runtime(
    thread_id: str,
    *,
    runtime,
    expected_owner: Any | None,
    destination_target: Any | None,
    runtime_context_epoch: int,
    expected_context_usage_floor: int | float | None,
    preserve_thread: bool,
    broadcast,
    operation_id: str | None,
) -> bool:
    if not preserve_thread or getattr(
        destination_target, "harness", None,
    ) != "claude-code":
        return await runtime.retire_if_current(
            thread_id,
            expected_owner=expected_owner,
            broadcast=broadcast,
            operation_id=operation_id,
        )
    transitioned = await runtime.retarget_if_current(
        thread_id,
        expected_owner=expected_owner,
        target=destination_target,
        runtime_context_epoch=runtime_context_epoch + 1,
        expected_context_usage_floor=expected_context_usage_floor,
    )
    if transitioned:
        return True
    log.warning(
        "Claude Code model retarget refused for thread_id=%s operation_id=%s; retiring exact owner",
        thread_id, operation_id or "unknown",
    )
    return await runtime.retire_if_current(
        thread_id,
        expected_owner=expected_owner,
        broadcast=broadcast,
        operation_id=operation_id,
        keep_resume_handle=True,
    )


async def _transition_codex_runtime(
    thread_id: str,
    *,
    runtime,
    expected_owner: Any | None,
    destination_target: Any | None,
    runtime_context_epoch: int,
    preserve_thread: bool,
    broadcast,
    operation_id: str | None,
) -> bool:
    if not preserve_thread or getattr(
        destination_target, "harness", None,
    ) != "codex":
        return await runtime.retire_if_current(
            thread_id,
            expected_owner=expected_owner,
            broadcast=broadcast,
            operation_id=operation_id,
        )
    transitioned = await runtime.retarget_if_current(
        thread_id,
        expected_owner=expected_owner,
        target=destination_target,
        runtime_context_epoch=runtime_context_epoch + 1,
    )
    if transitioned:
        return True
    log.warning(
        "Codex model retarget refused for thread_id=%s operation_id=%s; retiring exact owner",
        thread_id, operation_id or "unknown",
    )
    return await runtime.retire_if_current(
        thread_id,
        expected_owner=expected_owner,
        broadcast=broadcast,
        operation_id=operation_id,
        keep_resume_handle=True,
    )


async def _transition_source_runtime(
    thread_id: str,
    *,
    user_id: str | None,
    source_harness: str | None,
    source_runtime_owner: Any | None,
    destination_target: Any | None,
    source_runtime_context_epoch: int,
    expected_context_usage_floor: int | float | None,
    preserve_claude_thread: bool,
    preserve_codex_thread: bool,
    operation_id: str | None,
) -> bool:
    from infra import state
    from subagents import cc_runtime, codex_runtime

    broadcast = state.owner_broadcast(user_id) if user_id else None
    if source_harness == "claude-code":
        return await _transition_claude_runtime(
            thread_id,
            runtime=cc_runtime,
            expected_owner=source_runtime_owner,
            destination_target=destination_target,
            runtime_context_epoch=source_runtime_context_epoch,
            expected_context_usage_floor=expected_context_usage_floor,
            preserve_thread=preserve_claude_thread,
            broadcast=broadcast,
            operation_id=operation_id,
        )
    if source_harness == "codex":
        return await _transition_codex_runtime(
            thread_id,
            runtime=codex_runtime,
            expected_owner=source_runtime_owner,
            destination_target=destination_target,
            runtime_context_epoch=source_runtime_context_epoch,
            preserve_thread=preserve_codex_thread,
            broadcast=broadcast,
            operation_id=operation_id,
        )
    return True


async def _broadcast_model_switch_breadcrumb(
    thread_id: str,
    breadcrumb: dict | None,
) -> None:
    if not breadcrumb:
        return
    from infra.send_broadcast import broadcast_message_rows

    await broadcast_message_rows(thread_id, (breadcrumb["id"],))


def _account_switch_is_current(receipt) -> bool:
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute('SELECT t.model,t.runtime_context_epoch,b.account_id FROM threads t '
            'JOIN thread_vendor_accounts b ON b.thread_id=t.id AND b.provider=? WHERE t.id=?',
            (receipt['provider'], receipt['thread_id'])).fetchone()
        return row is not None and (row['model'], row['runtime_context_epoch'], row['account_id']) == (
            receipt['model'], receipt['runtime_context_epoch'], receipt['to_account_id'])
    finally:
        db.close()


async def post_account_switch_effects(receipt) -> None:
    """Publish the durable history once; late current-selection effects are fenced."""
    from infra.db_async import run_in_db

    await _broadcast_model_switch_breadcrumb(receipt['thread_id'], {'id': receipt['message_id']})
    if await run_in_db(_account_switch_is_current, receipt):
        _broadcast_model_selection(receipt['thread_id'], new_model=receipt['model'],
            user_id=receipt['user_id'], committed_at=receipt['updated_at'], account_update={
                'account_id': receipt['to_account_id'], 'previous_account_id': receipt['from_account_id'],
                'account_switch_reason': receipt['reason'], 'runtime_context_epoch': receipt['runtime_context_epoch']})


async def post_model_switch_effects(
    thread_id: str,
    *,
    new_model: str,
    user_id: str | None,
    source_harness: str | None,
    source_runtime_owner: Any | None,
    destination_target: Any | None = None,
    source_runtime_context_epoch: int = 0,
    expected_context_usage_floor: int | float | None = None,
    preserve_claude_thread: bool = False,
    preserve_codex_thread: bool = False,
    operation_id: str | None = None,
    breadcrumb: dict | None = None,
    committed_at: str | None = None,
) -> None:
    """Transition the exact source runtime, then publish the committed switch."""
    transitioned = await _transition_source_runtime(
        thread_id,
        user_id=user_id,
        source_harness=source_harness,
        source_runtime_owner=source_runtime_owner,
        destination_target=destination_target,
        source_runtime_context_epoch=source_runtime_context_epoch,
        expected_context_usage_floor=expected_context_usage_floor,
        preserve_claude_thread=preserve_claude_thread,
        preserve_codex_thread=preserve_codex_thread,
        operation_id=operation_id,
    )
    _broadcast_model_selection(
        thread_id,
        new_model=new_model,
        user_id=user_id,
        committed_at=committed_at,
    )
    emit_product_event(
        "model",
        "switched",
        thread_id=thread_id,
        user_id=user_id,
        properties={"model_id": new_model},
    )
    await _broadcast_model_switch_breadcrumb(thread_id, breadcrumb)
    if not transitioned:
        log.warning(
            "model switch committed but source runtime transition failed thread_id=%s operation_id=%s source_harness=%s",
            thread_id, operation_id or "unknown", source_harness or "unknown",
        )
        raise RuntimeError(
            "model switch source runtime owner changed or could not be transitioned"
        )


async def apply_switch_transition(transition) -> None:
    """Apply the provider-side effect of one FIFO switch commit."""
    if transition is None or not transition.committed:
        return
    from infra.db_async import run_in_db
    from llm.resolution import resolve_execution_target
    from subagents import cc_runtime, codex_runtime

    source = await run_in_db(resolve_execution_target, transition.source_model)
    destination = await run_in_db(
        resolve_execution_target, transition.destination_model,
    )
    source_harness = source.harness
    owner = None
    if source_harness == "claude-code":
        owner = await cc_runtime.capture_retirement_owner(transition.thread_id)
    elif source_harness == "codex":
        owner = await codex_runtime.capture_retirement_owner(transition.thread_id)
    await post_model_switch_effects(
        transition.thread_id,
        new_model=destination.catalog_id,
        user_id=None,
        source_harness=source_harness,
        source_runtime_owner=owner,
        destination_target=destination,
        operation_id=transition.operation_id,
        preserve_claude_thread=(
            source_harness == destination.harness == "claude-code"
        ),
        preserve_codex_thread=(source_harness == destination.harness == "codex"),
        committed_at=utc_now_iso(),
    )
