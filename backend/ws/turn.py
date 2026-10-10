"""Sole ingress-aware native turn engine."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from loguru import logger

from core.native_turn_control import TurnCancelEvent
from core.native_turn_runtime import (
    NativeTurn,
    make_runtime_bindings,
    native_provider_params,
    run_tracked_turn,
)
from core.step_handler import StepHandler
from core.turn_core import (
    TurnAlreadyActiveError,
    TurnIdentity,
    TurnThreadMissingError,
    TurnTerminalOwnershipError,
    bind_preadmitted_turn,
    persist_turn_start_bound,
    terminal_error_detail,
)
from core.turn_activity import turn_activity_frame
from infra.product_events import emit_product_event
from infra import state
from infra.time import utc_now
from llm.capabilities import make_provider_params
from llm.resolution import (
    ExecutionTarget,
    ModelUnavailableError,
    validate_native_target,
)
from ws.native_inbox_batch import native_safe_boundary_input as _native_safe_boundary_input
from ws.post_turn import run_post_turn_tasks
from ws.turn_cancellation import handle_cancelled_turn
from ws.turn_unavailable import report_model_unavailable
from ws.turn_types import _PreparedTurn, TurnBroadcast, TurnIngress
from ws.turn_helpers import (
    NativeTurnRequest,
    build_agent_content,
    emit_terminal_product_event,
    handle_turn_error,
    make_deferred_send,
    make_ws_send,
    record_browser_turn_activity,
    reject_future,
    resolve_future,
    settle_preadmitted_failure,
)


async def _setup_turn(
    thread_id: str,
    content: str,
    attachments: list[dict],
    *,
    target: ExecutionTarget,
    media_projection=None,
    turn_id: str,
    ingress: TurnIngress,
    reasoning_effort: str | None = None,
    broadcast: TurnBroadcast | None = None,
    step_metadata: dict | None = None,
    preadmitted_turn: TurnIdentity | None = None,
) -> _PreparedTurn:
    """Validate, prepare, and durably bind one admitted Native turn."""
    from core.agent import get_agent

    agent = await get_agent()
    await validate_native_target(target, event=ingress == "event")
    active_model = target.transport_model
    provider = native_provider_params(target, make_provider_params(active_model))
    if ingress == "event" and broadcast is None:
        raise ValueError("event ingress requires an owner broadcast")
    deferred_send, send_cell = make_deferred_send()
    agent_content, _image_paths = build_agent_content(content, attachments)
    handler = StepHandler(
        thread_id=thread_id,
        model_info={
            "model": active_model,
            "model_name": target.display_name,
            "is_cloud": target.backend == "api",
        },
        broadcast=deferred_send,
        turn_id=turn_id,
        extra_meta=step_metadata,
    )
    effective_effort = None if ingress == "event" else reasoning_effort
    if effective_effort and not target.supports_reasoning:
        effective_effort = None
    cancel_event = TurnCancelEvent()
    bind, unbind = make_runtime_bindings(
        thread_id,
        ingress,
        cancel_event,
        handler,
    )
    if preadmitted_turn is not None:
        if preadmitted_turn.turn_id != turn_id:
            raise ValueError("preadmitted Native Turn does not match turn_id")
        identity = preadmitted_turn
        await bind_preadmitted_turn(thread_id, identity, bind, unbind)
    else:
        identity = await persist_turn_start_bound(
            turn_id, thread_id, active_model, content, bind, unbind,
            cancel_event.completion,
        )
    return _PreparedTurn(
        agent,
        active_model,
        provider,
        cancel_event,
        identity,
        (deferred_send, send_cell),
        handler,
        agent_content,
        media_projection,
        unbind,
        effective_effort,
    )


async def _finish_failed_admission(
    prepared: _PreparedTurn, request: NativeTurnRequest, error: BaseException,
) -> None:
    """Settle failed admission and retire its runtime, including on failure."""
    terminal_failure: BaseException | None = None
    try:
        await settle_preadmitted_failure(
            request,
            error,
            prepared.identity,
            release=lambda: prepared.unbind(prepared.identity),
        )
    except BaseException as finalization_error:
        logger.exception("Failed admission terminalization for {}", prepared.identity.turn_id)
        terminal_failure = finalization_error
    prepared.unbind(prepared.identity)
    completion = prepared.cancel_event.completion
    if not completion.done():
        if terminal_failure is not None:
            completion.set_exception(terminal_failure)
        elif isinstance(error, asyncio.CancelledError):
            completion.set_result(True)
        else:
            completion.set_exception(error)
    if terminal_failure is not None:
        raise terminal_failure from error


async def _admit_prepared_turn(
    request: NativeTurnRequest,
    prepared: _PreparedTurn,
) -> TurnBroadcast:
    """Persist input, expose the broadcaster, and acknowledge one Turn."""
    try:
        if request.persist_input_once is not None:
            await request.persist_input_once()
        if request.after_persist_before_dispatch is not None:
            await request.after_persist_before_dispatch(request.target)

        deferred_send, send_cell = prepared.send_binding
        if request.ingress == "event":
            assert request.broadcast is not None
            send_cell[0] = request.broadcast
        else:
            send_cell[0] = make_ws_send(
                request.ws,
                request.user_id,
                request.thread_id,
                prepared.identity,
            )[0]
            record_browser_turn_activity(
                request.thread_id,
                request.user["id"],
                request.target.catalog_id or "",
            )

        await deferred_send(
            await turn_activity_frame(request.thread_id, prepared.identity)
        )
        resolve_future(request.start_ack, prepared.identity)
        emit_product_event(
            "turn",
            "started",
            thread_id=request.thread_id,
            user_id=request.user["id"],
            properties={
                "turn_id": prepared.identity.turn_id,
                "model": prepared.active_model,
                "has_attachments": bool(request.attachments),
                "is_retry": request.is_retry,
            },
        )
        return deferred_send
    except BaseException as error:
        await _finish_failed_admission(prepared, request, error)
        raise


async def _run_prepared_turn(
    request: NativeTurnRequest,
    prepared: _PreparedTurn,
) -> bool:
    """Run the provider loop for one fully admitted Native turn."""
    from infra.state import turn_buffers

    def _teardown(_cancelled: bool) -> None:
        turn_buffers.clear(request.thread_id, prepared.identity)

    async def _provider_accepted() -> None:
        resolve_future(request.provider_acceptance, True)
        if request.settle_delivery is not None:
            await request.settle_delivery()

    async def _safe_boundary(max_input_tokens: int):
        return await _native_safe_boundary_input(
            thread_id=request.thread_id,
            thread_row=request.thread_row,
            owner_turn_id=prepared.identity.turn_id,
            max_input_tokens=max_input_tokens,
        )

    return await run_tracked_turn(
        NativeTurn(
            agent=prepared.agent,
            thread_id=request.thread_id,
            content=prepared.agent_content,
            model=prepared.active_model,
            turn_provider=prepared.provider,
            handler=prepared.handler,
            cancel_event=prepared.cancel_event,
            identity=prepared.identity,
            media=prepared.media_projection,
            reasoning_effort=prepared.reasoning_effort,
            model_context_window=request.target.context_window,
            prepared_context=request.prepared_context,
            on_teardown=_teardown if request.ingress != "event" else None,
            on_provider_response=_provider_accepted,
            on_provider_terminal=request.settle_delivery_terminal,
            on_safe_boundary=_safe_boundary,
        )
    )


async def _execute_turn(request: NativeTurnRequest) -> bool:
    prepared: _PreparedTurn | None = None
    ws_send: TurnBroadcast | None = None
    product_terminal = False
    started_at = utc_now()
    try:
        prepared = _PreparedTurn(
            *await _setup_turn(
                request.thread_id,
                request.content,
                request.attachments,
                target=request.target,
                media_projection=request.media_projection,
                turn_id=request.turn_id,
                ingress=request.ingress,
                reasoning_effort=request.reasoning_effort,
                broadcast=request.broadcast,
                step_metadata=request.step_metadata,
                preadmitted_turn=request.preadmitted_turn,
            )
        )
        ws_send = await _admit_prepared_turn(request, prepared)
        cancelled = bool(await _run_prepared_turn(request, prepared))
        resolve_future(request.provider_acceptance, not cancelled)
        emit_terminal_product_event(
            request,
            prepared.identity,
            prepared.active_model,
            cancelled,
            started_at,
            emit_product_event,
        )
        product_terminal = True
        if cancelled:
            return True
        await run_post_turn_tasks(
            thread_id=request.thread_id,
            content=prepared.agent_content,
            ws_send=ws_send,
        )
        return False
    except (
        TurnAlreadyActiveError,
        TurnThreadMissingError,
        TurnTerminalOwnershipError,
    ) as error:
        reject_future(request.start_ack, error)
        resolve_future(request.provider_acceptance, False)
        raise
    except asyncio.CancelledError as error:
        await handle_cancelled_turn(
            request, error, prepared, product_terminal, started_at, emit_product_event,
            settle_preadmitted_failure,
        )
        raise
    except Exception as error:
        if prepared is None:
            if request.preadmitted_turn is not None:
                await settle_preadmitted_failure(request, error, request.preadmitted_turn)
            if isinstance(error, ModelUnavailableError):
                return await report_model_unavailable(request, error)
        handled = await handle_turn_error(
            request,
            prepared.identity if prepared else None,
            prepared.cancel_event if prepared else None,
            ws_send,
            product_terminal,
            error,
            emit_product_event,
        )
        if handled is None:
            raise
        return handled


async def execute_turn(
    ws,
    user: dict,
    user_id: str,
    thread_id: str,
    thread_row: object,
    content: str,
    attachments: list[dict],
    is_retry: bool,
    *,
    target: ExecutionTarget,
    turn_id: str,
    ingress: TurnIngress,
    reasoning_effort: str | None = None,
    start_ack: asyncio.Future | None = None,
    broadcast: TurnBroadcast | None = None,
    step_metadata: dict | None = None,
    persist_input_once: Callable[[], Awaitable[None]] | None = None,
    settle_delivery: Callable[[], Awaitable[None]] | None = None,
    after_persist_before_dispatch: (
        Callable[[ExecutionTarget], Awaitable[None]] | None
    ) = None,
    prepared_context=None,
    preadmitted_turn: TurnIdentity | None = None,
    provider_acceptance: asyncio.Future[bool] | None = None,
    settle_delivery_terminal: (
        Callable[[str, str | BaseException | None], Awaitable[None]] | None
    ) = None,
    media_projection=None,
) -> bool:
    """Execute one native turn for browser, delegation, or event ingress."""
    request = NativeTurnRequest(
        ws,
        user,
        user_id,
        thread_id,
        thread_row,
        content,
        attachments,
        is_retry,
        target,
        turn_id,
        ingress,
        reasoning_effort,
        start_ack,
        broadcast,
        step_metadata,
        persist_input_once,
        settle_delivery,
        after_persist_before_dispatch,
        prepared_context,
        preadmitted_turn,
        provider_acceptance,
        settle_delivery_terminal,
        media_projection,
    )
    try:
        return await _execute_turn(request)
    except BaseException as error:
        if settle_delivery_terminal is not None:
            outcome = "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
            detail = None if outcome == "cancelled" else terminal_error_detail(error)
            await settle_delivery_terminal(outcome, detail)
        raise
