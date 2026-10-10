"""Tracked process-local execution for one admitted Native turn."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

from loguru import logger

from core.turn_core import (
    terminal_error_detail,
    TurnIdentity as _TurnIdentity,
    TurnTerminalOwnershipError as _TurnTerminalOwnershipError,
    TurnTerminalResult as _TurnTerminalResult,
    commit_turn_terminal,
    drive_turn_settlement,
)
from core.checkpoint_native_admission import IDLE_RECEIPT_KEY, NativeIdleReceipt
from core.native_context_prefix import CONTEXT_WINDOW_UNSET
from core.turns import run_turn_with_steps
from infra import state


@dataclass
class NativeTurn:
    """One admitted Native turn ready for tracked execution."""

    agent: Any
    thread_id: str
    content: str
    model: str
    turn_provider: dict
    handler: Any
    cancel_event: Any
    identity: _TurnIdentity
    media: list | None = None
    reasoning_effort: str | None = None
    model_context_window: int | None | object = CONTEXT_WINDOW_UNSET
    prepared_context: Any | None = None
    on_teardown: Callable[[bool], None] | None = None
    on_provider_response: Callable[[], Awaitable[None] | None] | None = None
    on_provider_terminal: (
        Callable[[str, str | BaseException | None], Awaitable[None]] | None
    ) = None
    on_safe_boundary: Callable[[int], Awaitable[Any | None] | None] | None = None
    #: The final call's receipt, taken from the ``turn_complete`` terminal
    #: payload. Valid only for a Turn that completed on that call's measured
    #: answer and whose terminal claim this Turn wins; ``None`` otherwise. A
    #: later Turn supersedes it by clearing the idle marker it would ride on.
    idle_receipt: NativeIdleReceipt | None = None


def native_provider_params(target: Any, resolved: dict) -> dict:
    """Add resolved target identity to Native provider parameters."""
    source_provider = target.provider
    if target.provider == "external":
        routed_model = str(resolved.get("model") or "")
        routed_provider, separator, _model_id = routed_model.partition("/")
        source_provider = routed_provider if separator else "unknown"
    return {
        **resolved,
        "source_harness": target.harness,
        "source_provider": source_provider,
        "preserve_thinking_default": target.preserve_thinking_default,
    }


def make_runtime_bindings(
    thread_id: str,
    ingress: str,
    cancel_event: Any,
    handler: Any,
) -> tuple[
    Callable[[_TurnIdentity], None],
    Callable[[_TurnIdentity], None],
]:
    """Bind and unbind only one exact Native Turn's process-local handles."""
    from infra.state import turn_buffers

    turn_task = asyncio.current_task()

    def _bind(identity: _TurnIdentity) -> None:
        cancel_event.bind_turn_identity(identity)
        handler.bind_turn_identity(identity)
        state._active_turns[thread_id] = cancel_event
        if turn_task is not None:
            state._active_turn_tasks[thread_id] = turn_task
        if ingress != "event":
            turn_buffers.init(thread_id, identity)

    def _unbind(identity: _TurnIdentity) -> None:
        active = state._active_turns.get(thread_id)
        replacement_active = active is not None and active is not cancel_event
        if active is cancel_event:
            state._active_turns.pop(thread_id, None)
        if state._active_turn_tasks.get(thread_id) is turn_task:
            state._active_turn_tasks.pop(thread_id, None)
        if ingress != "event" and not replacement_active:
            turn_buffers.clear(thread_id, identity)

    return _bind, _unbind


def retire_failed_native_runtime(
    thread_id: str,
    ingress: str,
    cancel_event: Any | None,
    identity: _TurnIdentity | None,
) -> None:
    """Remove only the failed Turn's exact process-local handles."""
    active = state._active_turns.get(thread_id)
    owned_active = cancel_event is not None and active is cancel_event
    replacement_active = active is not None and active is not cancel_event
    if owned_active:
        state._active_turns.pop(thread_id, None)
    if state._active_turn_tasks.get(thread_id) is asyncio.current_task():
        state._active_turn_tasks.pop(thread_id, None)
    if ingress == "event" or not owned_active or replacement_active:
        return
    if identity is not None:
        from infra.state import turn_buffers

        turn_buffers.clear(thread_id, identity)


async def _run_native_turn(
    turn: NativeTurn,
    terminal_effects: dict[str, tuple[str, dict]],
) -> str:
    """Run one provider loop and select its truthful terminal outcome."""

    async def _handle_step(kind: str, payload: dict) -> Any:
        if kind in {"turn_complete", "turn_cancelled", "error"}:
            terminal_effects[kind] = (kind, _take_idle_receipt(turn, kind, payload))
            return None
        # The handler return is part of the step contract: tool projections use
        # the canonical persisted row returned by ``tool_complete``.
        return await turn.handler.handle(kind, payload)

    await run_turn_with_steps(
        agent=turn.agent,
        user_content=turn.content,
        thread_id=turn.thread_id,
        channel="web",
        chat_id=turn.thread_id,
        on_step=_handle_step,
        cancel_event=turn.cancel_event,
        model=turn.model,
        turn_provider=turn.turn_provider,
        media=turn.media,
        reasoning_effort=turn.reasoning_effort,
        model_context_window=turn.model_context_window,
        prepared_context=turn.prepared_context,
        on_provider_response=turn.on_provider_response,
        on_safe_boundary=turn.on_safe_boundary,
    )
    selected = {
        "turn_complete": "completed",
        "turn_cancelled": "cancelled",
        "error": "error",
    }
    if len(terminal_effects) == 1:
        return selected[next(iter(terminal_effects))]
    if not terminal_effects and turn.cancel_event.is_set():
        # Compatibility for runners that return without selecting an effect.
        return "cancelled"
    return "completed"


async def _claim_native_terminal(
    turn: NativeTurn,
    outcome: str,
    terminal_error: str | BaseException | None,
    quarantine: str | None = None,
) -> None:
    """Claim the durable terminal for the exact admitted Native generation."""
    settlement = await commit_turn_terminal(
        turn.identity.turn_id,
        outcome,
        terminal_error,
        quarantine=quarantine,
    )
    if settlement.result is not _TurnTerminalResult.APPLIED:
        raise _TurnTerminalOwnershipError(
            f"terminal owner rejected native turn {turn.identity.turn_id}: "
            f"{settlement.result.value}"
        )
    if state._active_turns.get(turn.thread_id) is turn.cancel_event:
        state._active_turns.pop(turn.thread_id, None)
    if state._active_turn_tasks.get(turn.thread_id) is asyncio.current_task():
        state._active_turn_tasks.pop(turn.thread_id, None)
    await drive_turn_settlement(_with_idle_receipt(turn, settlement))


def _take_idle_receipt(turn: NativeTurn, kind: str, payload: dict) -> dict:
    """Keep a completed Turn's receipt on the Turn; never publish it."""
    if kind != "turn_complete" or IDLE_RECEIPT_KEY not in payload:
        return payload
    published = dict(payload)
    turn.idle_receipt = published.pop(IDLE_RECEIPT_KEY)
    return published


def _with_idle_receipt(turn: NativeTurn, settlement):
    """Hand the receipt, named by the already-persisted answer row, to the idle timer."""
    receipt = turn.idle_receipt
    if receipt is None or settlement.idle_checkpoint is None or not turn.handler.step_ids:
        return settlement
    receipt = replace(receipt, final_message_id=str(turn.handler.step_ids[-1]))
    return replace(
        settlement,
        idle_checkpoint=replace(settlement.idle_checkpoint, native_receipt=receipt),
    )


def _selected_terminal_effect(
    turn: NativeTurn,
    outcome: str,
    terminal_effects: dict[str, tuple[str, dict]],
) -> tuple[str, dict] | None:
    """Return the CAS-owned terminal effect with exact replacement metadata."""
    effect_kind = {
        "completed": "turn_complete",
        "cancelled": "turn_cancelled",
        "error": "error",
    }[outcome]
    effect = terminal_effects.get(effect_kind)
    if effect_kind != "turn_cancelled":
        return effect
    payload = dict(effect[1]) if effect is not None else {}
    if getattr(turn.cancel_event, "replacement_requested", False):
        payload["_replacement_requested"] = True
    return effect_kind, payload


async def _publish_terminal_effects(
    turn: NativeTurn,
    outcome: str,
    terminal_effects: dict[str, tuple[str, dict]],
) -> None:
    """Publish effects that only the winning durable terminal may emit."""
    effect = _selected_terminal_effect(turn, outcome, terminal_effects)
    if effect is not None:
        await turn.handler.handle(*effect)


async def _finalize_native_turn(
    turn: NativeTurn,
    outcome: str,
    terminal_error: str | BaseException | None,
    terminal_effects: dict[str, tuple[str, dict]],
) -> None:
    """Settle provider writes, then always claim and publish the terminal.

    A failing provider-terminal callback must not leave the Turn running with
    no owner (#1688). The writes it left unsettled become ambiguous, as boot
    recovery records them, and its failure is raised after the claim.
    """
    callback_failure: BaseException | None = None
    if turn.on_provider_terminal is not None:
        try:
            await turn.on_provider_terminal(outcome, terminal_error)
        except BaseException as exc:
            callback_failure = exc
    try:
        await _claim_native_terminal(
            turn, outcome, terminal_error,
            None if callback_failure is None
            else "delivery settlement failed after provider dispatch",
        )
        await _publish_terminal_effects(turn, outcome, terminal_effects)
    finally:
        if callback_failure is not None:
            raise callback_failure


def _retire_native_runtime(
    turn: NativeTurn,
    outcome: str,
    execution_failure: BaseException | None,
    finalization_failure: BaseException | None,
) -> None:
    """Retire only this generation's exact process-local runtime handles."""
    active_handle = state._active_turns.get(turn.thread_id)
    replacement_active = (
        active_handle is not None and active_handle is not turn.cancel_event
    )
    if active_handle is turn.cancel_event:
        state._active_turns.pop(turn.thread_id, None)
    if state._active_turn_tasks.get(turn.thread_id) is asyncio.current_task():
        state._active_turn_tasks.pop(turn.thread_id, None)
    if turn.on_teardown is not None and not replacement_active:
        turn.on_teardown(outcome == "cancelled")
    completion = getattr(turn.cancel_event, "completion", None)
    if completion is None or completion.done():
        return
    if finalization_failure is not None:
        completion.set_exception(finalization_failure)
    elif execution_failure is not None and not isinstance(
        execution_failure, asyncio.CancelledError
    ):
        completion.set_exception(execution_failure)
    else:
        completion.set_result(outcome == "cancelled")


async def run_tracked_turn(turn: NativeTurn) -> bool:
    """Run, durably terminalize once, and retire the exact runtime."""
    outcome = "completed"
    terminal_error: str | BaseException | None = None
    terminal_effects: dict[str, tuple[str, dict]] = {}
    execution_failure: BaseException | None = None
    try:
        outcome = await _run_native_turn(turn, terminal_effects)
    except BaseException as exc:
        execution_failure = exc
        outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
        terminal_error = None if outcome == "cancelled" else terminal_error_detail(exc)
        raise
    finally:
        finalization_failure: BaseException | None = None
        try:
            await _finalize_native_turn(turn, outcome, terminal_error, terminal_effects)
        except BaseException as exc:
            finalization_failure = exc
            raise
        finally:
            _retire_native_runtime(
                turn,
                outcome,
                execution_failure,
                finalization_failure,
            )
    return outcome == "cancelled"
