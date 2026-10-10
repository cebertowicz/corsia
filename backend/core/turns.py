"""Instrumented entry point for the Native turn iteration loop."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Awaitable, Callable

from loguru import logger

from core import turn_iteration
from core.native_context_prefix import CONTEXT_WINDOW_UNSET
from core.turns_instrument import set_attrs, turn_span
from llm.capabilities import make_provider_params

if TYPE_CHECKING:
    from core.media_projection import ValidatedMediaProjection


async def run_turn_with_steps(
    agent, user_content: str, thread_id: str, channel: str, chat_id: str,
    on_step: Callable, cancel_event: asyncio.Event, *, model: str,
    turn_provider=None, media: list[str] | ValidatedMediaProjection | None = None,
    fresh_context: bool = False, reasoning_effort: str | None = None,
    model_context_window: int | None | object = CONTEXT_WINDOW_UNSET,
    prepared_context=None,
    on_provider_response: Callable[[], Awaitable[None] | None] | None = None,
    on_safe_boundary: Callable[
        [int], Awaitable[turn_iteration.NativeSafeBoundaryInput | None] | None
    ] | None = None,
) -> None:
    """Run a Native turn on its own ``model``; ``fresh_context`` omits history.

    The model is the turn's, never borrowed: admission holds every input of a
    thread without one (``input_admission``) until the user chooses a model,
    so a call without one is an invariant violation, not a user path.
    """
    turn_model = (model or "").strip()
    if not turn_model:
        raise RuntimeError(
            "Invariant violated: a Native turn started without its model. "
            "Admission holds a model-less thread's inputs until a model is "
            "chosen, so every caller must pass the turn's model."
        )
    provider_params = turn_provider if turn_provider is not None else make_provider_params(turn_model)
    logger.info(
        "run_turn_with_steps: starting for session={}, model={}, fresh_context={}",
        thread_id, turn_model, fresh_context,
    )
    with turn_span(thread_id=thread_id, turn_model=turn_model, fresh_context=fresh_context) as span:
        iteration, tools_used = await turn_iteration.run_native_iteration(
            agent, user_content, thread_id, channel, chat_id, on_step, cancel_event,
            model=turn_model, turn_provider=provider_params, media=media,
            fresh_context=fresh_context, reasoning_effort=reasoning_effort,
            model_context_window=model_context_window, prepared_context=prepared_context,
            on_provider_response=on_provider_response, on_safe_boundary=on_safe_boundary,
        )
        set_attrs(span, {
            "turn.iteration_count": iteration,
            "turn.tools_used": tools_used,
        })


__all__ = ["run_turn_with_steps"]
