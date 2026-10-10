"""Queued input a Native Turn claims at a safe tool boundary, and its release."""

from __future__ import annotations

import asyncio
import inspect

from loguru import logger

from core import turn_iteration_context as iteration_context


async def resolve_unconsumed_boundary_input(state, thread_id: str) -> None:
    """Release pre-write claims; quarantine claims whose provider write began."""
    if state.active_boundary_input is None:
        return
    callback = (state.active_boundary_input.mark_ambiguous if state.boundary_write_started
                else state.active_boundary_input.release_before_write)
    try:
        await iteration_context.await_transition(callback)
    except Exception:
        logger.exception("native safe-boundary delivery cleanup failed for session={}", thread_id)
    finally:
        state.active_boundary_input = None
        state.boundary_write_started = False


async def claim_safe_boundary(state, thread_id: str, turn_identity,
                              cancel_event: asyncio.Event, on_safe_boundary) -> bool:
    remaining = iteration_context.remaining_native_input(state)
    try:
        candidate = on_safe_boundary(remaining)
        if inspect.isawaitable(candidate):
            candidate = await candidate
    except Exception:
        logger.exception("native safe-boundary lookup failed for session={}", thread_id)
        candidate = None
    if candidate is None:
        return False
    state.active_boundary_input = candidate
    state.messages.append({"role": "user", "content": candidate.content})
    state.fit_current_input = candidate.content
    state.fit_ledger = iteration_context.refit_provider_payload(
        state.fit_ledger, state.messages, state.tool_definitions,
        model=state.turn_model,
        input_identity=candidate.input_identity, current_input=candidate.content)
    return False
