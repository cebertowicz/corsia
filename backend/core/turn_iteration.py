"""Phase sequencing for one Native turn iteration loop."""

from __future__ import annotations

import asyncio
import inspect
from copy import deepcopy
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from core import turn_iteration_context as iteration_context
from core import turn_provider_call, turns_steps
from core.turn_provider_error import (
    TurnProviderError, check_provider_error, end_if_overflow_unchanged, terminal_payload as _terminal_payload,
)
from core.checkpoint_native_admission import IDLE_RECEIPT_KEY, freeze_idle_receipt
from core.native_context_prefix import CONTEXT_WINDOW_UNSET, _freeze_tool_definitions
import tools.confined_reads as confined_reads
from core.tool_result_projection import HARNESS_TOOL_RESULT_BUDGET_TOKENS
from core.turn_context import prepare_turn_context
from core.turn_safe_boundary import claim_safe_boundary, resolve_unconsumed_boundary_input
from core.turns_util import _log_llm_usage, usage_call_fields
from plans.public import binding_at_call_start
from infra import state as runtime_state
from infra.db_async import run_in_db

if TYPE_CHECKING:
    from core.media_projection import ValidatedMediaProjection


class ProviderDeliverySettlementError(RuntimeError):
    """Provider output whose durable user-delivery predecessor did not settle."""


NativeSafeBoundaryInput = iteration_context.NativeSafeBoundaryInput


@dataclass(slots=True)
class _IterationState:
    """Only the mutable locals formerly held by the monolithic loop body."""

    messages: list[dict[str, Any]]
    context_usage: dict
    tool_definitions: list[dict[str, Any]]
    context_identity: Any
    fit_ledger: Any
    fit_current_input: Any
    checkpoint_source_messages: list[dict[str, Any]]
    turn_model: str
    iteration: int = 0
    final_content: str | None = None
    tools_used: list[str] = field(default_factory=list)
    last_measured_call_usage: dict | None = None
    last_measured_rendered_input_tokens: int | None = None
    terminal_effect_emitted: bool = False
    provider_delivery_settled: bool = False
    provider_delivery_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_boundary_input: NativeSafeBoundaryInput | None = None
    boundary_write_started: bool = False
    restart_resume_registered: bool = False
    # (window, response) of the one overflow this Turn sends back to the boundary.
    overflowed: tuple[int | None, Any] | None = None
    overflow_retry_pending: bool = False


async def _prepare_iteration_state(
    agent, thread_id: str, user_content: str, turn_model: str, fresh_context: bool,
    project_id: str | None, channel: str, chat_id: str, media, model_context_window, prepared_context,
    thread_context=None,
) -> _IterationState:
    if prepared_context is None:
        tool_definitions = _freeze_tool_definitions(await confined_reads.advertised(agent.tools, thread_context))
        messages, context_usage = await prepare_turn_context(
            agent, thread_id, user_content, turn_model, fresh_context, project_id,
            channel, chat_id, media, model_context_window=model_context_window,
            tool_definitions=tool_definitions,
        )
    else:
        messages = deepcopy(list(prepared_context.messages))
        context_usage = deepcopy(prepared_context.context_usage)
        tool_definitions = deepcopy(list(prepared_context.tool_definitions))
    context_identity = getattr(prepared_context, "context_identity", None)
    logger.info("run_turn_with_steps: built {} LLM messages (sanitized), entering loop", len(messages))
    return _IterationState(
        messages, context_usage, tool_definitions, context_identity, fit_ledger=getattr(prepared_context, "fit_ledger", None),
        fit_current_input=iteration_context.current_fit_input(messages),
        checkpoint_source_messages=[], turn_model=turn_model)


async def _settle_provider_delivery(state: _IterationState, on_provider_response) -> None:
    """Fence every provider-originated effect behind durable user delivery."""
    async with state.provider_delivery_lock:
        try:
            if not state.provider_delivery_settled and on_provider_response is not None:
                await iteration_context.await_transition(on_provider_response)
            state.provider_delivery_settled = True
            if state.active_boundary_input is not None:
                await iteration_context.await_transition(state.active_boundary_input.settle_delivery)
                state.active_boundary_input = None
                state.boundary_write_started = False
        except BaseException as exc:
            raise ProviderDeliverySettlementError(
                "provider output withheld because durable user delivery could not be settled") from exc


async def _provider_step(state, on_provider_response, on_step, kind: str, payload: dict):
    await _settle_provider_delivery(state, on_provider_response)
    return await on_step(kind, payload)


async def _stop_at_restart_boundary(state: _IterationState, cancel_event: asyncio.Event, turn_identity, thread_id: str) -> bool:
    """Durably preserve unfinished work before stopping for restart."""
    if not getattr(cancel_event, "restart_pending", False):
        return False
    if turn_identity is not None and not state.restart_resume_registered:
        from automation.restart_wakes import register_restart_resume

        try:
            await run_in_db(register_restart_resume, thread_id, turn_identity.turn_id)
        except RuntimeError:
            # Rollback can cancel control while controlled -> pending waits. If
            # in-memory intent cleared, this healthy Turn continues.
            if not getattr(cancel_event, "restart_pending", False):
                state.restart_resume_registered = False
                return False
            raise
        state.restart_resume_registered = True
    # Restart cancellation may race registration. Rollback owns cancellation
    # of that durable registration, and the healthy Turn keeps running.
    if not getattr(cancel_event, "restart_pending", False):
        state.restart_resume_registered = False
        return False
    setattr(cancel_event, "restart_boundary_stopped", True)
    cancel_event.set()
    return True


async def _call_provider(
    agent, state: _IterationState, turn_model: str, provider_params: dict, thread_id: str,
    cancel_event: asyncio.Event, provider_on_step, reasoning_effort: str | None, on_provider_response):
    if state.active_boundary_input is not None and not state.boundary_write_started:
        await iteration_context.await_transition(state.active_boundary_input.before_provider_write)
        state.boundary_write_started = True
    state.checkpoint_source_messages = deepcopy(state.messages)
    call_binding = await run_in_db(binding_at_call_start, thread_id)
    response, tools_already_streamed = await turn_provider_call.call_llm(
        agent, state.messages, state.tool_definitions, turn_model, provider_params,
        thread_id, cancel_event, provider_on_step, state.iteration, reasoning_effort=reasoning_effort,
        on_response_started=partial(_settle_provider_delivery, state, on_provider_response),
        destination_fit_ledger=state.fit_ledger,
        destination_fit_input_identity=(state.fit_ledger.input_identity
                                        if state.fit_ledger is not None else ""),
        destination_fit_current_input=state.fit_current_input)
    if response is None:
        cancel_event.set()
        return None
    await _settle_provider_delivery(state, on_provider_response)
    measured = iteration_context._effective_input_tokens(response.usage)
    if response.finish_reason != "error" and measured is not None:
        # Provider measurement wins before best-effort telemetry, but an error
        # never displaces the last successful occupancy measurement.
        state.last_measured_call_usage = dict(response.usage)
        if state.fit_ledger is not None:
            state.last_measured_rendered_input_tokens = int(
                state.fit_ledger.rendered_input_tokens
            )
        usage = iteration_context._context_usage_from_latest_call(
            state.context_usage, state.last_measured_call_usage,
            include_context_details=False)
        await provider_on_step("context_usage", {"context_usage": usage, "_context_usage_identity": state.context_identity})
    await run_in_db(
        _log_llm_usage, thread_id, turn_model, response.usage,
        harness=provider_params.get("source_harness"),
        **usage_call_fields(response, state.iteration, call_binding,
                                getattr(response, "effort_sent", None)),
    )
    return response, tools_already_streamed


async def _run_tool_phase(
    agent, state: _IterationState, response, tools_already_streamed: bool,
    thread_id: str, cancel_event: asyncio.Event, provider_on_step, turn_identity, on_safe_boundary) -> bool:
    # Restart intent arriving during inference stops before new tool effects.
    if await _stop_at_restart_boundary(state, cancel_event, turn_identity, thread_id):
        return True
    checkpoint_prefix_size = len(state.messages)
    state.messages = await turns_steps.execute_tool_calls(
        agent, response, state.messages, thread_id, cancel_event,
        provider_on_step, state.tools_used, model=state.turn_model,
        tools_already_streamed=tools_already_streamed,
        tool_result_budget_tokens=HARNESS_TOOL_RESULT_BUDGET_TOKENS)
    # Keep the resumable Assistant Step, excluding tool results and provider-only
    # media, by using the known append boundary rather than trailing role guesses.
    state.checkpoint_source_messages = deepcopy(state.messages[:checkpoint_prefix_size + 1])
    if not cancel_event.is_set() and state.iteration < agent.max_iterations and state.fit_ledger is not None:
        state.fit_ledger = iteration_context.refit_provider_payload(
            state.fit_ledger, state.messages, state.tool_definitions,
            model=state.turn_model,
            input_identity=state.fit_ledger.input_identity, current_input=state.fit_current_input)
    # An admitted tool batch settles before restart stops another provider call.
    if await _stop_at_restart_boundary(state, cancel_event, turn_identity, thread_id):
        return True
    if cancel_event.is_set() or state.iteration >= agent.max_iterations or on_safe_boundary is None:
        return False
    return await claim_safe_boundary(state, thread_id, turn_identity, cancel_event, on_safe_boundary)


async def _emit_terminal(state: _IterationState, agent, thread_id: str, cancel_event: asyncio.Event,
                         turn_identity, on_step, provider_on_step) -> None:
    if state.final_content is None and not cancel_event.is_set() and state.iteration >= agent.max_iterations:
        state.final_content = (
            f"I reached the maximum number of tool call iterations ({agent.max_iterations}) "
            "without completing the task. You can try breaking the task into smaller steps.")
        await provider_on_step("text", {"content": state.final_content})
    state.terminal_effect_emitted = True
    if (cancel_event.is_set() and getattr(cancel_event, "restart_pending", False)
            and not state.restart_resume_registered and turn_identity is not None):
        from automation.restart_wakes import register_hard_recovery

        await run_in_db(register_hard_recovery, thread_id, turn_identity.turn_id)
    if cancel_event.is_set():
        # A local cancellation before the first response has no acceptance proof;
        # publishing its terminal must not turn a queued input into delivered.
        await on_step("turn_cancelled", _terminal_payload(state))
    else:
        await on_step("turn_complete", _terminal_payload(
            state, tools_used=state.tools_used, **{IDLE_RECEIPT_KEY: freeze_idle_receipt(state)}))


async def _finish_text_response(state, agent, response, provider_on_step) -> None:
    state.final_content = agent._strip_think(response.content)
    if state.final_content is not None:
        await provider_on_step("text", {"content": state.final_content})
        return
    state.terminal_effect_emitted = True
    message = "The model returned an empty response. Retry, or switch models if it persists."
    await provider_on_step("error", _terminal_payload(state, message=message))
    raise TurnProviderError(message)


async def _raise_iteration_exception(state, cancel_event, on_step, exc) -> None:
    stopped = bool(getattr(cancel_event, "user_stop_requested", False))
    if not state.terminal_effect_emitted:
        payload = _terminal_payload(state) if stopped else _terminal_payload(state, message=str(exc) or type(exc).__name__)
        await on_step("turn_cancelled" if stopped else "error", payload)
        if not stopped:
            setattr(exc, "_corsia_terminal_effect_emitted", True)
    if stopped:
        raise asyncio.CancelledError from exc
    raise exc


async def _run_loop(
    agent, state: _IterationState, thread_id: str, turn_model: str,
    provider_params: dict, cancel_event: asyncio.Event, on_step, reasoning_effort,
    on_provider_response, on_safe_boundary, turn_identity, project_id: str | None
) -> tuple[int, list[str]]:
    provider_on_step = partial(_provider_step, state, on_provider_response, on_step)
    try:
        while state.iteration < agent.max_iterations:
            if await _stop_at_restart_boundary(state, cancel_event, turn_identity, thread_id):
                break
            if cancel_event.is_set():
                break
            prepared = await iteration_context.maintain_native_checkpoint(
                agent=agent,
                state=state,
                thread_id=thread_id,
                turn_identity=turn_identity,
                cancel_event=cancel_event,
                turn_model=turn_model,
                reasoning_effort=reasoning_effort,
                project_id=project_id,
            )
            if prepared is not None:
                iteration_context.adopt_native_context(state, prepared)
            await end_if_overflow_unchanged(state, prepared, turn_model, provider_on_step)
            state.iteration += 1
            result = await _call_provider(agent, state, turn_model, provider_params,
                thread_id, cancel_event, provider_on_step, reasoning_effort, on_provider_response)
            if result is None:
                break
            response, tools_already_streamed = result
            if await check_provider_error(
                    state, response, turn_model, thread_id, provider_on_step, state.iteration < agent.max_iterations):
                continue
            await turns_steps.emit_reasoning(response, provider_on_step,
                was_streamed=provider_params.get("uses_streaming", False))
            if response.has_tool_calls:
                if await _run_tool_phase(
                    agent, state, response, tools_already_streamed, thread_id,
                    cancel_event, provider_on_step, turn_identity, on_safe_boundary):
                    break
            else:
                await _finish_text_response(state, agent, response, provider_on_step)
                break
        await _emit_terminal(state, agent, thread_id, cancel_event, turn_identity, on_step, provider_on_step)
        return state.iteration, state.tools_used
    except asyncio.CancelledError:
        if not state.terminal_effect_emitted:
            await on_step("turn_cancelled", _terminal_payload(state))
        raise
    except ProviderDeliverySettlementError:
        # Keep output quarantined without its causal input row.
        raise
    except BaseException as exc:
        await _raise_iteration_exception(state, cancel_event, on_step, exc)


async def run_native_iteration(
    agent, user_content: str, thread_id: str, channel: str, chat_id: str,
    on_step: Callable, cancel_event: asyncio.Event, *, model: str, turn_provider: dict,
    media: list[str] | ValidatedMediaProjection | None = None, fresh_context: bool = False,
    reasoning_effort: str | None = None, model_context_window: int | None | object = CONTEXT_WINDOW_UNSET,
    prepared_context=None,
    on_provider_response: Callable[[], Awaitable[None] | None] | None = None,
    on_safe_boundary: Callable[[int], Awaitable[NativeSafeBoundaryInput | None] | None] | None = None,
) -> tuple[int, list[str]]:
    """Run the existing Native phases and return span-level loop attributes."""
    from infra.db_async import run_in_db
    from infra.queries import get_native_turn_thread_context

    row = await run_in_db(get_native_turn_thread_context, thread_id)
    thread_context = dict(row) if row is not None else None
    project_id = thread_context.get("project_id") if thread_context else None
    thread_token = runtime_state._current_thread_id.set(thread_id)
    turn_identity = iteration_context.bound_native_turn_identity(cancel_event, prepared_context)
    turn_token = runtime_state._current_turn_identity.set(turn_identity)
    iteration_state = None
    try:
        await agent._connect_mcp()
        iteration_state = await _prepare_iteration_state(agent, thread_id, user_content,
            model, fresh_context, project_id, channel, chat_id, media, model_context_window, prepared_context,
            thread_context)
        from core.agent_checkpoint_runtime import bound_native_agent_checkpoint_runtime

        with bound_native_agent_checkpoint_runtime(
            iteration_state, thread_id, turn_identity, reasoning_effort,
        ):
            return await _run_loop(
                agent, iteration_state, thread_id, model, turn_provider, cancel_event,
                on_step, reasoning_effort, on_provider_response, on_safe_boundary, turn_identity, project_id)
    finally:
        try:
            if iteration_state is not None:
                await resolve_unconsumed_boundary_input(iteration_state, thread_id)
        finally:
            runtime_state._current_turn_identity.reset(turn_token)
            runtime_state._current_thread_id.reset(thread_token)
