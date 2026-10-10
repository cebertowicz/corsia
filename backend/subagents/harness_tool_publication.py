"""Post-effect publication for the shared external-harness tool bridge."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from tools.recorded_call import CompletedToolCall, CompletedToolFailure, completed_tool_failure
from tools.tool_surface import resolve_dispatch

if TYPE_CHECKING:
    from core._tool_result_delivery_support import ToolExecutionResult
    from core.turn_core import TurnIdentity


class ProjectedMediaResult(str):
    """Model-facing text carrying media already approved for one target."""

    media: Any
    completion: CompletedToolCall | None

    def __new__(cls, value: str, media: Any, completion: CompletedToolCall | None = None):
        instance = super().__new__(cls, value)
        instance.media = media
        instance.completion = completion
        return instance


class ToolBridgeResult(ProjectedMediaResult):
    """Model-facing projection carrying its canonical durable binding."""

    delivery: "ToolExecutionResult"
    turn_identity: "TurnIdentity | None"

    def __new__(
        cls,
        value: str,
        delivery: "ToolExecutionResult",
        turn_identity: "TurnIdentity | None" = None,
        media: Any | None = None,
        completion: CompletedToolCall | None = None,
    ):
        instance = super().__new__(cls, value, media, completion)
        instance.delivery = delivery
        instance.turn_identity = turn_identity
        return instance


async def _checkpoint_result(
    completion: CompletedToolCall,
    model: str | None,
    *,
    harness: str = "external",
) -> str:
    """Project private checkpoint media while retaining post-effect failure."""
    reference = getattr(completion.result, "media", None)
    if reference is None:
        return completion.result
    try:
        from core.media_delivery import tool_result_media_for_reference

        media_content = await asyncio.to_thread(
            tool_result_media_for_reference,
            str(completion.result),
            reference,
            model,
        )
        return ProjectedMediaResult(media_content.text, media_content, completion)
    except CompletedToolFailure:
        raise
    except (Exception, asyncio.CancelledError) as error:
        raise completed_tool_failure((completion,), error, harness=harness) from error


async def _foreground_result(
    completion: CompletedToolCall,
    *,
    thread_id: str | None,
    turn_identity: "TurnIdentity | None",
    model: str | None,
    persist_delivery,
    media_delivery,
    decorate_result,
    harness: str = "external",
) -> str:
    """Persist and project one foreground result through existing callables."""
    try:
        if (
            not isinstance(thread_id, str)
            or not thread_id
            or turn_identity is None
            or not completion.invocation_id
        ):
            from core._tool_result_delivery_support import ToolResultBindingError

            raise ToolResultBindingError(
                "harness tool execution lacks session, Turn, or invocation identity"
            )
        delivery = await persist_delivery(
            thread_id=thread_id,
            turn_identity=turn_identity,
            invocation_id=completion.invocation_id,
            provider_tool_call_id=completion.provider_tool_call_id,
            assistant_step_id=completion.assistant_step_id,
            name=completion.name,
            arguments=completion.arguments,
            content=completion.result,
        )
        completion = replace(
            completion, canonical=delivery, assistant_step_id=delivery.assistant_step_id,
        )
        from core.tool_result_projection import HARNESS_TOOL_RESULT_BUDGET_TOKENS

        media_content = await media_delivery(delivery, model)
        projected = (
            media_content.text
            if media_content is not None
            else delivery.render_for_model(
                thread_id=thread_id, budget_tokens=HARNESS_TOOL_RESULT_BUDGET_TOKENS,
            )
        )
        resolved_name, resolved_arguments, _ = resolve_dispatch(
            completion.name, completion.arguments,
        )
        projected = await decorate_result(
            projected,
            delivery,
            thread_id=thread_id,
            turn_identity=turn_identity,
            provider_tool_call_id=completion.provider_tool_call_id,
            name=resolved_name,
            arguments=resolved_arguments,
        )
        return ToolBridgeResult(
            projected, delivery, turn_identity, media_content, completion,
        )
    except CompletedToolFailure:
        raise
    except asyncio.CancelledError as error:
        if completion.canonical is not None:
            raise
        raise completed_tool_failure((completion,), error, harness=harness) from error
    except Exception as error:
        raise completed_tool_failure((completion,), error, harness=harness) from error


async def publish_completed_tool_result(
    completion: CompletedToolCall,
    *,
    thread_id: str | None,
    turn_identity: "TurnIdentity | None",
    checkpoint_id: str | None,
    model: str | None,
    persist_delivery,
    media_delivery,
    decorate_result,
    harness: str = "external",
) -> str:
    """Publish a completed effect without creating another execution owner."""
    if checkpoint_id is not None:
        return await _checkpoint_result(completion, model, harness=harness)
    return await _foreground_result(
        completion,
        thread_id=thread_id,
        turn_identity=turn_identity,
        model=model,
        persist_delivery=persist_delivery,
        media_delivery=media_delivery,
        decorate_result=decorate_result,
        harness=harness,
    )
