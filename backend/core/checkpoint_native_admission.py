"""Warm-or-cold admission for a Native checkpoint boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from infra.db_async import run_off_db


#: The ``turn_complete`` terminal payload key that carries the receipt to the
#: Native terminal owner, which removes it before anything is published.
IDLE_RECEIPT_KEY = "_idle_receipt"


@dataclass(frozen=True, slots=True)
class NativeIdleReceipt:
    """The final call's exact input plus its answer, and the answer's row.

    ``generation_id`` is the installed checkpoint the input was built on; a
    fold may only claim that generation as its base.
    """

    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...]
    provider_input_tokens: int
    rendered_input_tokens: int
    generation_id: str | None
    final_message_id: str | None = None


def system_input_tokens(state) -> int:
    """The Turn's measured fixed prompt, as the initial render recorded it."""
    return int(dict(state.context_usage.get("fit_section_tokens") or {}).get("fixed") or 0)


def effective_input_tokens(usage: dict | None) -> int | None:
    """Normalize one call without double-counting OpenAI-compatible cache."""
    if not isinstance(usage, dict):
        return None
    for field in ("effective_input_tokens", "prompt_tokens"):
        value = usage.get(field)
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    return None


def has_correlated_receipt(state) -> bool:
    """Whether the Turn holds the measured call receipt a warm fork needs."""
    rendered = state.last_measured_rendered_input_tokens
    return bool(
        state.checkpoint_source_messages
        and effective_input_tokens(state.last_measured_call_usage) is not None
        and type(rendered) is int
        and rendered >= 0
    )


def skip_optional_cold_fold(state, required: bool) -> None:
    """Refuse an optional fold that could only go cold for want of a receipt.

    Before the Turn's first call, or after a call that reported no usage, a
    later boundary folds warm from the next measured call. A required fold
    still runs cold.
    """
    if not required and not has_correlated_receipt(state):
        from core.checkpoint_types import OptionalCheckpointSkipped

        raise OptionalCheckpointSkipped("no correlated receipt for a warm fold")


def freeze_idle_receipt(state) -> NativeIdleReceipt | None:
    """Freeze the final call's receipt when the Turn ended on that call's answer.

    A tool step or the iteration cap leaves the shorter resumable slice, and
    each tool phase refits the ledger, so an older measurement mismatches.
    """
    ledger = state.fit_ledger
    provider_tokens = effective_input_tokens(state.last_measured_call_usage)
    if (
        provider_tokens is None
        or not has_correlated_receipt(state)
        or state.final_content is None
        or ledger is None
        or ledger.rendered_input_tokens != state.last_measured_rendered_input_tokens
        or state.checkpoint_source_messages != state.messages
    ):
        return None
    answer = {"role": "assistant", "content": state.final_content}
    return NativeIdleReceipt(
        messages=(*state.checkpoint_source_messages, answer),
        tools=tuple(state.tool_definitions),
        provider_input_tokens=provider_tokens,
        rendered_input_tokens=state.last_measured_rendered_input_tokens,
        generation_id=state.context_usage.get("checkpoint_generation_id"),
    )


async def capture_native_checkpoint_source(
    thread_id, state, context, reasoning_effort,
):
    """Freeze exact live warmth only when its correlated receipt is present."""
    from core.fork_native import capture_live_warm_source

    if has_correlated_receipt(state):
        measured = effective_input_tokens(state.last_measured_call_usage)
        rendered = state.last_measured_rendered_input_tokens
        return capture_live_warm_source(
            thread_id, context.boundary,
            messages=state.checkpoint_source_messages,
            tools=state.tool_definitions,
            reasoning_effort=reasoning_effort,
            preserve_thinking=context.target.preserve_thinking_default,
            provider_input_tokens=measured,
            rendered_input_tokens=rendered,
        )
    from core.fork_capture import warm_capture_refused

    return warm_capture_refused(thread_id, "native", "no_correlated_receipt")


def build_native_cold_checkpoint_admission(
    checkpoint_id, thread_id, context, turn_identity, reasoning_effort,
    trigger="pressure", system_tokens=0,
):
    """Build the existing Native cold request from one frozen DB context."""
    from dataclasses import replace

    from core.checkpoint_cold_admission import build_checkpoint_admission
    from infra.database import get_db

    db = get_db()
    try:
        admission = build_checkpoint_admission(
            checkpoint_id=checkpoint_id,
            thread_id=thread_id,
            trigger=trigger,
            source_target=context.target,
            writer_target=context.target,
            boundary=context.boundary,
            previous=context.previous,
            projection=context.projection,
            working_dir=context.working_dir,
            reasoning_effort=reasoning_effort,
            binary=None,
            originating_turn_id=turn_identity.turn_id,
            db=db,
        )
        return replace(admission, provider_request=replace(
            admission.provider_request, system_tokens=system_tokens,
        ))
    finally:
        db.close()


async def prepare_native_checkpoint(
    checkpoint_id, thread_id, state, context, turn_identity, reasoning_effort,
    *, trigger="pressure",
):
    """Use proven live warmth when available; otherwise freeze a cold request."""
    source = await capture_native_checkpoint_source(
        thread_id, state, context, reasoning_effort,
    )
    if source is not None:
        from dataclasses import replace
        from functools import partial

        from core.checkpoint_warm_request import build_warm_admission

        admission = build_warm_admission(
            checkpoint_id=checkpoint_id, thread_id=thread_id,
            turn=turn_identity, trigger=trigger, target=context.target,
            boundary=context.boundary,
            previous_generation_id=context.previous_generation_id,
            working_dir=context.working_dir, reasoning_effort=reasoning_effort,
            binary=None, warm_source=source,
        )
        system_tokens = system_input_tokens(state)
        # Warmth proved by a past receipt can still exceed the window on the
        # next call; carry this boundary's own cold admission so the fork can
        # correct its path rather than ending the turn.
        return replace(
            admission,
            provider_request=replace(
                admission.provider_request,
                system_tokens=system_tokens,
                same_model_cold=partial(
                    run_off_db,
                    build_native_cold_checkpoint_admission,
                    checkpoint_id, thread_id, context, turn_identity,
                    reasoning_effort, trigger, system_tokens,
                ),
            ),
        )
    # A local writer's cold count asks its own server; that wait holds no DB worker.
    return await run_off_db(
        build_native_cold_checkpoint_admission,
        checkpoint_id, thread_id, context, turn_identity, reasoning_effort, trigger,
        system_input_tokens(state),
    )
