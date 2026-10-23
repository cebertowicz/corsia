"""Per-step primitives for the CORSIA turn loop."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger

from infra.db_async import run_in_db
from infra.queries import get_thread_project_id
from infra.state.context import tool_invocation_context

from infra.delivery_states import USER_INTERRUPTION_REASON
from core.turns_instrument import tool_loop_span
from tools.tool_surface import dispatch_metadata
from tools.recorded_call import (
    CompletedToolCall,
    CompletedToolFailure,
    completed_tool_failure,
    enrich_completed_failure,
)

USER_INTERRUPTION_TOOL_RESULT = (
    "This tool call was cancelled by user interruption. If it is still relevant "
    "after the interruption, run it again."
)

MISSING_TOOL_RESULT_TEXT = (
    "No result was recorded before the turn ended; side effects are unknown. "
    "Verify current state before assuming the tool did not run."
)

async def lookup_thread_project_id(thread_id: str) -> str | None:
    """Look up the advisory ``project_id`` for a thread."""
    import sqlite3
    try:
        return await run_in_db(get_thread_project_id, thread_id)
    except (sqlite3.Error, RuntimeError) as e:
        logger.warning(
            "lookup_thread_project_id failed for thread_id={!r}: {!r}",
            thread_id, e,
        )
    return None


async def emit_reasoning(response, on_step, *, was_streamed: bool = False) -> None:
    """Emit reasoning content from response metadata or inline think tags."""
    from llm.think_tags import extract_think_content

    if not was_streamed:
        reasoning = getattr(response, "reasoning_content", None)
        provenance = getattr(response, "reasoning_provenance", None)
        if reasoning or provenance is not None:
            payload = {"content": reasoning}
            if provenance is not None:
                payload["provenance"] = provenance
            await on_step("reasoning", payload)
            return

    think_content = extract_think_content(response.content)
    if think_content:
        await on_step("reasoning", {"content": think_content})


def build_tool_call_payloads(agent, response):
    """Build UI-facing and LLM-facing payloads from response tool calls."""
    tool_infos = []
    tool_call_dicts = []
    for tc in response.tool_calls:
        tool_infos.append({
            "id": tc.id,
            "name": tc.name,
            "hint": agent._tool_hint([tc]),
            "args": json.dumps(tc.arguments, ensure_ascii=False, indent=2) if tc.arguments else "",
        })
        tool_call_dicts.append({
            "id": tc.id,
            "type": "function",
            "function": {
                "name": tc.name,
                "arguments": json.dumps(tc.arguments, ensure_ascii=False),
            },
        })
    return tool_infos, tool_call_dicts


async def execute_tool_calls(
    agent, response, messages, thread_id,
    cancel_event, on_step, tools_used, model,
    tools_already_streamed=False,
    tool_result_budget_tokens=None,
    checkpoint_generation_id: str | None = None,
):
    """Execute tool calls, emit step events, and stitch results into messages."""
    with tool_loop_span(
        tool_count=len(response.tool_calls),
        tool_names=[tc.name for tc in response.tool_calls],
    ):
        return await _execute_tool_calls_inner(
            agent, response, messages, thread_id,
            cancel_event, on_step, tools_used, model,
            tools_already_streamed=tools_already_streamed,
            tool_result_budget_tokens=tool_result_budget_tokens,
            checkpoint_generation_id=checkpoint_generation_id,
        )


async def _execute_tool_calls_inner(
    agent, response, messages, thread_id,
    cancel_event, on_step, tools_used, model,
    tools_already_streamed=False,
    tool_result_budget_tokens=None,
    checkpoint_generation_id: str | None = None,
):
    """Body of ``execute_tool_calls`` — extracted so the OTel span wraps cleanly."""
    clean_text = agent._strip_think(response.content)
    tool_infos, tool_call_dicts = build_tool_call_payloads(agent, response)

    # The persisted Step owns every call in this batch, even when a later
    # result write fails before returning its canonical row.
    step_id = await on_step("tool_calls_start", {
        "tools": tool_infos,
        "content": clean_text or "",
        "tool_call_dicts": tool_call_dicts,
        "skip_broadcast": tools_already_streamed,
    })
    step_id = step_id if isinstance(step_id, str) else None

    messages = agent.context.add_assistant_message(
        messages, response.content, tool_call_dicts,
        reasoning_content=response.reasoning_content,
    )

    executed_tc_ids: set[str] = set()
    canonical_results: list[tuple[Any, str, Any | None]] = []
    for tc in response.tool_calls:
        if cancel_event.is_set():
            break
        dispatch = dispatch_metadata(tc.name, tc.arguments)
        tools_used.append(dispatch.get("tool_name", tc.name))
        try:
            result = await _execute_native_tool(agent, tc, checkpoint_generation_id)
        except CompletedToolFailure as failure:
            identity = _current_turn_identity()
            enrich_completed_failure(
                failure,
                prefix=_completed_native_calls(canonical_results, thread_id, step_id),
                name=tc.name,
                arguments=tc.arguments,
                thread_id=thread_id,
                turn_id=identity.turn_id if identity else None,
                invocation_id=tc.id,
                provider_tool_call_id=tc.id,
                assistant_step_id=step_id,
            )
            raise
        executed_tc_ids.add(tc.id)
        result = _end_preview(tc.id, result or "")
        canonical_results.append((tc, result, None))
        try:
            canonical = await on_step(
                "tool_complete",
                {"id": tc.id, "name": tc.name, "result": result},
            )
        except Exception as error:
            raise completed_tool_failure(
                _completed_native_calls(canonical_results, thread_id, step_id),
                error, harness="native",
            ) from error
        canonical_results[-1] = (tc, result, canonical)

    if cancel_event.is_set():
        await _settle_unexecuted_calls(response, executed_tc_ids, on_step, canonical_results)

    try:
        await on_step("tool_calls_done", {"tool_ids": [tc.id for tc in response.tool_calls]})
        return await _append_tool_results(
            agent, messages, canonical_results, thread_id, tool_result_budget_tokens, model,
        )
    except Exception as error:
        completed = _completed_native_calls(
            [row for row in canonical_results if row[0].id in executed_tc_ids], thread_id,
            step_id,
        )
        if not completed:
            raise
        raise completed_tool_failure(completed, error, harness="native") from error


async def _execute_native_tool(agent, tc, checkpoint_generation_id: str | None):
    """Run one native tool call in its provider and installed-generation context."""
    with tool_invocation_context(
        tc.id, provider_call_id=tc.id, checkpoint_generation_id=checkpoint_generation_id,
    ):
        return await agent.tools.execute(tc.name, tc.arguments)


async def _settle_unexecuted_calls(response, executed_tc_ids, on_step, canonical_results):
    """Close each call a cancelled batch never ran with the interruption result."""
    for tc in response.tool_calls:
        if tc.id not in executed_tc_ids:
            canonical = await on_step("tool_complete", {
                "id": tc.id, "name": tc.name,
                "result": USER_INTERRUPTION_TOOL_RESULT,
                "synthetic_tool_result": True,
                "synthetic_reason": USER_INTERRUPTION_REASON,
            })
            canonical_results.append((tc, USER_INTERRUPTION_TOOL_RESULT, canonical))


def _completed_native_calls(canonical_results, thread_id, step_id):
    """Retain a whole completed batch without inventing a missing durable row."""
    identity = _current_turn_identity()
    return tuple(CompletedToolCall(
        name=tc.name, result=result, arguments=dict(tc.arguments),
        thread_id=thread_id,
        turn_id=getattr(canonical, "turn_id", None) or (identity.turn_id if identity else None),
        invocation_id=tc.id,
        provider_tool_call_id=tc.id,
        assistant_step_id=getattr(canonical, "assistant_step_id", None) or step_id,
        canonical=canonical,
    ) for tc, result, canonical in canonical_results)


def _end_preview(call_id: str, result):
    """An executed call ends its streamed preview; a live write already ended it at its commit. Returns the result."""
    from doceditor.live_preview import previews
    from infra.tool_result_errors import is_error_result

    previews.end(call_id, "failed" if is_error_result(result) else "settled")
    return result


def _current_turn_identity():
    """Return the exact task-local Turn, if this call is owned by one."""
    from infra import state

    return state._current_turn_identity.get(None)


async def _append_tool_results(
    agent, messages, canonical_results, thread_id, tool_result_budget_tokens, model
):
    """Attach tool results without materializing media on the turn event loop."""
    return await asyncio.to_thread(
        _append_tool_results_sync,
        agent,
        messages,
        canonical_results,
        thread_id,
        tool_result_budget_tokens,
        model,
    )


def _append_tool_results_sync(
    agent, messages, canonical_results, thread_id, tool_result_budget_tokens, model
):
    """Build native tool-result context in a worker thread."""
    from core.context_builder import (
        enforce_native_image_request_limits,
        native_message_image_count,
    )
    from core.media_reads import IMAGE_KIND, media_reference_from_metadata
    from core.plan_notice import BatchPlanNotice

    image_count = native_message_image_count(messages) + sum(
        reference is not None and reference.kind == IMAGE_KIND
        for reference in (
            media_reference_from_metadata(
                getattr(canonical, "media_metadata", None)
            )
            for _tc, _result, canonical in canonical_results
        )
    )
    media_images = []
    # The Turn's first work call may sit in any batch, so each batch reads it once.
    plan_notices = BatchPlanNotice(thread_id)
    for tc, result, canonical in canonical_results:
        projected = result
        media = None
        if tool_result_budget_tokens is not None:
            if getattr(canonical, "role", None) != "tool":
                raise RuntimeError(
                    "Native tool projection requires its canonical persisted row"
                )
            from core.tool_result_projection import render_tool_result_for_model

            projected = render_tool_result_for_model(
                thread_id,
                canonical,
                budget_tokens=tool_result_budget_tokens,
            )
            media = _native_tool_media(
                canonical, model, image_count=image_count
            )
            if media is not None:
                projected = media.text
            from core.plan_notice import with_notice
            from core.repo_staleness_notice import repo_staleness_notice
            from events.connectors.github_pr_notice import pr_event_notice
            from tools.tool_surface import resolve_dispatch

            dispatched_name, dispatched_arguments, _ = resolve_dispatch(
                tc.name, tc.arguments,
            )
            projected = with_notice(projected, plan_notices.for_result(
                turn_id=getattr(canonical, "turn_id", None),
                tool_call_id=tc.id,
                tool_name=dispatched_name,
            ))
            projected = with_notice(projected, repo_staleness_notice(
                thread_id=thread_id,
                tool_name=dispatched_name,
                arguments=dispatched_arguments,
                workspace=agent.workspace,
            ))
            projected = with_notice(projected, pr_event_notice(
                thread_id=thread_id, tool_name=dispatched_name, arguments=dispatched_arguments))
        if media is not None and media.image is not None:
            media_images.append(media.image)
        messages = agent.context.add_tool_result(
            messages,
            tc.id,
            tc.name,
            projected,
        )
    if media_images:
        messages = agent.context.add_tool_media(messages, media_images)
    return enforce_native_image_request_limits(messages, model)


def _native_tool_media(canonical, model, *, image_count: int = 1):
    """Resolve native media through the canonical delivery owner."""
    from core.media_delivery import tool_result_media_for_model

    return tool_result_media_for_model(
        canonical, model, image_count=image_count
    )


__all__ = [
    "lookup_thread_project_id",
    "emit_reasoning",
    "build_tool_call_payloads",
    "execute_tool_calls",
]
