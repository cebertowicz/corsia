"""How one Native turn ends on a provider-reported error."""

from __future__ import annotations

from typing import Any

from loguru import logger

from core import turn_iteration_context as iteration_context
from infra import state as runtime_state
from llm.error_cause import response_error_fields


class TurnProviderError(RuntimeError):
    """Provider-reported turn failure already surfaced by the step handler."""


def terminal_payload(state, **fields) -> dict:
    """The terminal step payload carrying the latest measured context usage."""
    fields["context_usage"] = iteration_context._context_usage_from_latest_call(state.context_usage, state.last_measured_call_usage)
    if state.context_identity is not None:
        fields["_context_usage_identity"] = state.context_identity
    return fields


async def check_provider_error(state, response, turn_model, thread_id, provider_on_step, retry_available: bool) -> bool:
    """End the Turn on a provider error; True when a first overflow goes back to the boundary.

    ``retry_available`` is whether an iteration remains for the retry: without one the refusal ends the Turn here.
    """
    if response.finish_reason != "error":
        return False
    turn = getattr(runtime_state._current_turn_identity.get(None), "turn_id", None)
    logger.warning("run_turn_with_steps: LLM returned error for session {} turn {} kind={}: {}",
                   thread_id, turn, getattr(response, "error_kind", None), response.content)
    overflow = getattr(response, "error_kind", None) == "provider_context_overflow"
    if overflow and state.overflowed is None and retry_available:
        # The refused call emitted no step; the boundary re-resolves the window and
        # must checkpoint before the one retry.
        state.overflowed = response
        state.overflow_retry_pending = True
        return True
    await end_on_provider_error(state, response, turn_model, provider_on_step, overflow)
    return False


async def end_on_provider_error(state, response, turn_model, provider_on_step, overflow: bool) -> None:
    """Emit the terminal error step and raise it."""
    state.terminal_effect_emitted = True
    if overflow:
        # The provider's raw sentence is not the handled I7 transition copy.
        message = (
            "The provider rejected this turn for size, so the context window "
            f"for {turn_model} was re-measured and refreshed. Retry the send; the next turn is "
            "fit against the live window.")
    else:
        message = response.content or (
            "The model call failed without a diagnostic message. Retry, or "
            "switch models if it persists.")
    await provider_on_step("error", terminal_payload(
        state, message=message, **response_error_fields(response)))
    raise TurnProviderError(message)


async def end_if_overflow_unchanged(state, prepared, turn_model, provider_on_step) -> None:
    """End the Turn, handled, when the one retry would resend what the provider refused.

    The retry boundary must adopt a checkpoint (``maintain_native_checkpoint``), so
    no prepared continuation means the next call would carry the refused payload.
    """
    if not state.overflow_retry_pending:
        return
    state.overflow_retry_pending = False
    if prepared is None:
        await end_on_provider_error(state, state.overflowed, turn_model, provider_on_step, True)
