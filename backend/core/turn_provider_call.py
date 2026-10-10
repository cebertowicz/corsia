"""Direct owner for one Native turn provider call."""

from __future__ import annotations

import asyncio

from loguru import logger

from infra import state
from llm import chat_completion
from llm.tool_input_stream import tool_input_sink

from core.turns_instrument import llm_span, set_attrs


class _StreamingCallbacks:
    """Per-call streaming callbacks and first-seen tool tracking."""

    def __init__(self, on_step, thread_id: str) -> None:
        self.on_step = on_step
        self.thread_id = thread_id
        self.streamed_tool_indices: set[int] = set()
        self.workspace = None
        self.preview_calls: set[str] = set()

    def on_tool_input(self, call_id: str, name: str, fragment: str) -> None:
        """Feed a file tool's streamed input to the document preview; synchronous, no database."""
        from doceditor.live_preview import preview_tool, previews

        tool = preview_tool(name)
        if tool is None:
            return
        if call_id not in self.preview_calls:
            self.preview_calls.add(call_id)
            turn = state._current_turn_identity.get()
            previews.start(call_id, thread_id=self.thread_id, turn_id=getattr(turn, "turn_id", None),
                           tool=tool, workspace=self.workspace)
        previews.update(call_id, fragment)

    def on_tool_abandoned(self, call_id: str) -> None:
        """An attempt the adapter retries or turns into an error never runs this call: end its preview now."""
        from doceditor.live_preview import previews

        if call_id in self.preview_calls:
            previews.end(call_id, "failed")

    def end_previews(self, outcome: str) -> None:
        """A call that never returned its response ends every preview it started."""
        from doceditor.live_preview import previews

        for call_id in self.preview_calls:
            previews.end(call_id, outcome)

    async def on_reasoning_chunk(self, chunk: str) -> None:
        await self.on_step("reasoning", {"content": chunk})

    async def on_content_chunk(self, chunk: str) -> None:
        await self.on_step("text_delta", {"content": chunk})

    async def on_tool_call_chunk(
        self,
        idx: int,
        tc_id: str,
        name: str,
        args: str,
    ) -> None:
        if idx in self.streamed_tool_indices:
            return
        await self.on_step("tool_start", {
            "thread_id": self.thread_id,
            "tool_id": tc_id,
            "name": name,
            "hint": "",
            "args": args,
        })
        self.streamed_tool_indices.add(idx)


def _prepare_provider_call(turn_model, provider_params, on_step, thread_id):
    """Resolve the routed model and per-call streaming state."""
    return (
        provider_params.get("model", turn_model),
        provider_params.get("uses_streaming", False),
        _StreamingCallbacks(on_step, thread_id),
    )


def _start_provider_call(
    agent,
    messages,
    role_tool_defs,
    turn_model,
    provider_params,
    thread_id,
    reasoning_effort,
    destination_fit_ledger,
    destination_fit_input_identity,
    destination_fit_current_input,
    on_response_started,
    routed_model,
    use_streaming,
    callbacks,
):
    """Create the exact provider task for this Native iteration."""
    return asyncio.create_task(chat_completion(
        messages=messages,
        tools=role_tool_defs,
        model=routed_model,
        api_key=provider_params.get("api_key"),
        api_base=provider_params.get("api_base"),
        omlx_server_id=provider_params.get("omlx_server_id"),
        on_reasoning_chunk=(
            callbacks.on_reasoning_chunk if use_streaming else None
        ),
        on_content_chunk=callbacks.on_content_chunk if use_streaming else None,
        on_tool_call_chunk=(
            callbacks.on_tool_call_chunk if use_streaming else None
        ),
        on_response_started=on_response_started if use_streaming else None,
        reasoning_effort=reasoning_effort,
        reasoning_levels=provider_params.get("reasoning_levels"),
        thread_id=thread_id,
        preserve_thinking=provider_params.get("preserve_thinking_default"),
        destination_fit_ledger=destination_fit_ledger,
        destination_fit_target_catalog_id=(
            destination_fit_ledger.target_catalog_id
            if destination_fit_ledger is not None
            else turn_model
        ),
        destination_fit_input_identity=destination_fit_input_identity,
        destination_fit_current_input=destination_fit_current_input,
    ))


def _response_span_attributes(response) -> dict[str, object]:
    """Build the existing usage and TTFT attributes for the LLM span."""
    usage = response.usage or {}
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    cached_tokens = int(usage.get("cache_read_tokens", 0) or 0)
    attributes: dict[str, object] = {
        "llm.has_tool_calls": bool(response.has_tool_calls),
        "llm.finish_reason": response.finish_reason or "",
        "llm.prompt_tokens": prompt_tokens,
        "llm.completion_tokens": int(
            usage.get("completion_tokens", 0) or 0
        ),
        "llm.cached_tokens": cached_tokens,
        "llm.prefill_new_tokens": prompt_tokens,
    }
    ttft = getattr(response, "ttft_ms", None)
    if ttft is not None:
        attributes["llm.ttft_ms"] = float(ttft)
    return attributes


async def _finish_provider_call(
    response,
    provider_params,
    use_streaming,
    callbacks,
    span,
) -> None:
    """Record result evidence and normalize provider reasoning provenance."""
    logger.info(
        "run_turn_with_steps: got response, "
        f"has_tool_calls={response.has_tool_calls}, "
        f"content_len={len(response.content or '')}"
    )
    set_attrs(span, _response_span_attributes(response))
    if not (
        getattr(response, "reasoning_content", None)
        or getattr(response, "reasoning_provenance", None) is not None
    ):
        return

    from core.reasoning_provenance import normalize_reasoning_provenance

    response.reasoning_provenance = normalize_reasoning_provenance(
        response.reasoning_provenance,
        source_harness=provider_params.get("source_harness"),
        source_provider=provider_params.get("source_provider"),
        source_effective_model=getattr(response, "reported_model", None),
    )
    if use_streaming:
        await callbacks.on_step("reasoning", {
            "content": "",
            "provenance": response.reasoning_provenance,
        })


async def call_llm(
    agent, messages, role_tool_defs, turn_model, provider_params,
    thread_id, cancel_event, on_step, iteration,
    reasoning_effort=None, on_response_started=None,
    destination_fit_ledger=None,
    destination_fit_input_identity="",
    destination_fit_current_input=None,
):
    """Issue one LLM call with streaming callbacks and cancellation support."""
    routed_model, use_streaming, callbacks = _prepare_provider_call(
        turn_model, provider_params, on_step, thread_id,
    )
    with llm_span(
        model=routed_model,
        iteration=iteration,
        streaming=bool(use_streaming),
    ) as span:
        # The workspace the file tools are registered with, which their paths resolve in.
        callbacks.workspace = getattr(agent, "workspace", None) if use_streaming else None
        # The provider task copies this binding when it is created, so it is unbound right after.
        sink_binding = tool_input_sink.set(callbacks if use_streaming else None)
        try:
            chat_task = _start_provider_call(
                agent, messages, role_tool_defs, turn_model, provider_params,
                thread_id, reasoning_effort, destination_fit_ledger,
                destination_fit_input_identity, destination_fit_current_input, on_response_started,
                routed_model, use_streaming, callbacks,
            )
        finally:
            tool_input_sink.reset(sink_binding)
        state._active_llm_tasks[thread_id] = chat_task
        if cancel_event.is_set():
            # Stop sets the event before it looks for a provider task to cancel,
            # so a task registered after that signal must cancel itself here.
            chat_task.cancel()
        logger.info(
            "run_turn_with_steps: iteration "
            f"{iteration}, calling chat_completion with model={turn_model}"
        )
        try:
            response = await chat_task
            await _finish_provider_call(
                response, provider_params, use_streaming, callbacks, span,
            )
            return response, bool(callbacks.streamed_tool_indices)
        except asyncio.CancelledError:
            logger.info(
                f"run_turn_with_steps: LLM call cancelled for session {thread_id}"
            )
            cancel_event.set()
            callbacks.end_previews("stopped")
            return None, False
        except Exception as llm_err:
            callbacks.end_previews("failed")
            logger.error(
                "run_turn_with_steps: LLM call failed for session "
                f"{thread_id}: {llm_err}"
            )
            raise
        finally:
            if state._active_llm_tasks.get(thread_id) is chat_task:
                state._active_llm_tasks.pop(thread_id, None)


__all__ = ["call_llm"]
