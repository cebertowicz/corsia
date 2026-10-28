"""LLM router and shared response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable

import litellm

from llm.error_cause import mark_own_server

litellm.suppress_debug_info = True

litellm.drop_params = False


def _resolve_output_budget(model: str, requested: int | None) -> dict[str, Any]:
    """Return the caller-owned output budget, or nothing when none was asked for.

    Sampling and output limits are the provider's settings: CORSIA sends no
    temperature or other sampling knob, and an absent budget stays absent
    instead of being invented from the catalog (#1043, #1287). Only a caller
    that bounds its own job (a checkpoint) sends ``max_tokens``, and the
    provider-published output limit is the only ceiling applied to it.
    """
    from llm.capabilities import get_model_max_output_tokens

    if requested is None:
        return {}
    return {"max_tokens": min(
        get_model_max_output_tokens(model, fallback=requested), requested
    )}


def _is_transient_error(e: Exception) -> bool:
    """Check if an error is transient and worth retrying."""
    from llm.stream_idle import PROVIDER_CUT_OFF_ERRORS

    if isinstance(e, PROVIDER_CUT_OFF_ERRORS):
        return True
    err_str = str(e).lower()
    transient_patterns = [
        "overloaded",
        "rate_limit",
        "rate limit",
        "too many requests",
        "connection reset",
        "cannot connect",
        "server disconnected",
        "internal server error",
        "service unavailable",
        "timeout",
        "529",
        "503",
        "502",
    ]
    return any(p in err_str for p in transient_patterns)


@dataclass
class ToolCallRequest:
    """Tool call request from an LLM."""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """Response from an LLM call."""
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_content: str | None = None
    reasoning_provenance: dict[str, Any] | None = None
    ttft_ms: float | None = None
    reported_model: str | None = None
    # Machine classification for handled terminal failures. A classified
    # response carries no user-facing content; the turn owner composes the
    # handled transition (e.g. "provider_context_overflow" after window
    # rediscovery, I7) instead of surfacing a provider sentence.
    error_kind: str | None = None
    # ``auth`` when the failure proves the endpoint refuses this key;
    # read only by the endpoint observer (``llm.server_observation``).
    endpoint_failure: str | None = None
    # The reasoning level the dispatcher that built the request put on the
    # wire, None when it sent none (unlisted level, "auto", "none"): the usage
    # ledger records this, not the level that was requested.
    effort_sent: str | None = None
    # When a usage-limit failure's reset time is known (ISO), else None.
    resets_at: str | None = None
    # True when the failing oMLX server is CORSIA's own (set by chat_completion).
    own_server: bool = False

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


def provider_error_response(error: Exception, content: str) -> LLMResponse:
    """The terminal response for a provider call that failed with ``error``."""
    from llm.error_cause import USAGE_LIMIT, native_error_kind, reset_iso
    from llm.server_observation import endpoint_failure_of
    from llm.stream_idle import error_kind_of

    error_kind = error_kind_of(error) or native_error_kind(error)
    return LLMResponse(
        content=content, finish_reason="error", error_kind=error_kind,
        endpoint_failure=endpoint_failure_of(error),
        resets_at=reset_iso(error) if error_kind == USAGE_LIMIT else None,
    )


def _omlx_prompt_extras(
    messages: list[dict[str, Any]],
    reasoning_effort: str | None,
    preserve_thinking: bool | None,
    model: str,
) -> dict[str, Any]:
    """Return the oMLX extra_body fields for this turn, effort included.

    The level travels inside ``extra_body`` on purpose: litellm's per-provider
    allowlist rejects ``reasoning_effort`` as a top-level parameter for a
    generic OpenAI-compatible model (``drop_params`` is off), while extra_body
    is merged into the request JSON verbatim, so the chat template reads the
    same field either way. The level is validated against the model's reported
    vocabulary first; see ``wire_reasoning_effort``.
    """
    from llm.providers.omlx import make_prompt_extra_body
    from llm.reasoning_config import reasoning_config_for_model

    return make_prompt_extra_body(
        messages,
        reasoning_effort,
        preserve_thinking=preserve_thinking,
        reasoning_config=reasoning_config_for_model(model),
    )


def _listed_effort(
    reasoning_effort: str | None, reasoning_levels: list[str] | None,
) -> dict[str, Any] | None:
    """Top-level ``reasoning_effort`` for an External endpoint, or nothing.

    ``reasoning_levels`` is what the endpoint's provider publishes for this
    model (``build_external_params``). Only a level in that list is sent; any
    other value, "auto" included, sends nothing and the provider's default
    runs. It rides in ``extra_body`` for the reason given in
    ``_omlx_prompt_extras``, and lands top-level in the request JSON.
    """
    if reasoning_effort and reasoning_levels and reasoning_effort in reasoning_levels:
        return {"reasoning_effort": reasoning_effort}
    return None


def _enforce_destination_fit(
    thread_id: str | None,
    ledger: Any,
    target_catalog_id: str | None,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    input_identity: str,
    current_input: Any,
) -> None:
    """Run the pre-call destination-fit gate for a threaded turn."""
    from core.destination_fit import require_provider_call_fit

    require_provider_call_fit(
        ledger,
        harness="native",
        target_catalog_id=target_catalog_id or model,
        accounting_payload={"messages": messages, "tools": tools or []},
        input_identity=input_identity,
        current_input=current_input,
    )


async def _dispatch_openai(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    model: str,
    api_key: str | None,
    output_budget: dict[str, Any],
    reasoning_effort: str | None,
    on_reasoning_chunk: Any,
    on_content_chunk: Any,
    on_tool_call_chunk: Any,
    on_response_started: Any,
    retry_transient_errors: bool,
) -> LLMResponse:
    """Delegate an ``openai/`` model to the Responses API client."""
    from llm.providers.openai import openai_completion
    return await openai_completion(
        messages, tools,
        model=model,
        api_key=api_key, **output_budget,
        reasoning_effort=reasoning_effort,
        on_reasoning_chunk=on_reasoning_chunk,
        on_content_chunk=on_content_chunk,
        on_tool_call_chunk=on_tool_call_chunk, on_response_started=on_response_started,
        retry_transient_errors=retry_transient_errors,
    )


async def _dispatch_anthropic(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    model: str,
    api_key: str | None,
    api_base: str | None,
    output_budget: dict[str, Any],
    reasoning_effort: str | None,
    on_reasoning_chunk: Any,
    on_content_chunk: Any,
    on_tool_call_chunk: Any,
    on_response_started: Any,
    retry_transient_errors: bool,
) -> LLMResponse:
    """Delegate an ``anthropic/`` model to the Anthropic client."""
    from llm.providers.anthropic import anthropic_completion
    return await anthropic_completion(
        messages, tools,
        model=model,
        api_key=api_key, api_base=api_base, **output_budget,
        reasoning_effort=reasoning_effort,
        on_reasoning_chunk=on_reasoning_chunk,
        on_content_chunk=on_content_chunk,
        on_tool_call_chunk=on_tool_call_chunk, on_response_started=on_response_started,
        retry_transient_errors=retry_transient_errors,
    )


async def _observed(api_base: str | None, dispatch: Awaitable[LLMResponse]) -> LLMResponse:
    """Return one dispatch's outcome unchanged, handing a copy to the endpoint observer.

    Every External dispatch passes here; the observer records it in a separate
    task (``llm.server_observation``), so nothing it does reaches the turn.
    """
    from llm.server_observation import observe_dispatch

    try:
        response = await dispatch
    except Exception as exc:
        observe_dispatch(api_base, exc)
        raise
    observe_dispatch(api_base, response)
    return response


async def chat_completion(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    model: str,
    max_tokens: int | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
    omlx_server_id: str | None = None,
    on_reasoning_chunk: Any = None,
    on_content_chunk: Any = None,
    on_tool_call_chunk: Any = None, on_response_started: Any = None,
    reasoning_effort: str | None = None, reasoning_levels: list[str] | None = None,
    thread_id: str | None = None,
    preserve_thinking: bool | None = None,
    retry_transient_errors: bool = True,
    destination_fit_ledger=None,
    destination_fit_target_catalog_id: str | None = None,
    destination_fit_input_identity: str = "",
    destination_fit_current_input: Any = None,
) -> LLMResponse:
    """Route to provider-specific implementation based on model prefix."""
    if not model or not str(model).strip():
        raise ValueError(
            "chat_completion requires a non-empty `model`. Pass the operation's "
            "own model input (the thread's model target) — never call with an "
            "empty model, and never rely on a runtime default."
        )
    if thread_id is not None:
        _enforce_destination_fit(
            thread_id, destination_fit_ledger, destination_fit_target_catalog_id,
            model, messages, tools, destination_fit_input_identity,
            destination_fit_current_input,
        )
    requested_max_tokens = (
        max(1, int(max_tokens)) if max_tokens is not None else None
    )

    is_omlx = api_key == "omlx"

    output_budget = _resolve_output_budget(model, requested_max_tokens)

    if model.startswith("openai/") and api_base is None:
        return await _dispatch_openai(
            messages, tools, model=model, api_key=api_key, output_budget=output_budget,
            reasoning_effort=reasoning_effort,
            on_reasoning_chunk=on_reasoning_chunk, on_content_chunk=on_content_chunk,
            on_tool_call_chunk=on_tool_call_chunk, on_response_started=on_response_started,
            retry_transient_errors=retry_transient_errors,
        )

    if model.startswith("anthropic/"):
        return await _observed(api_base, _dispatch_anthropic(
            messages, tools, model=model, api_key=api_key, api_base=api_base,
            output_budget=output_budget, reasoning_effort=reasoning_effort,
            on_reasoning_chunk=on_reasoning_chunk, on_content_chunk=on_content_chunk,
            on_tool_call_chunk=on_tool_call_chunk, on_response_started=on_response_started,
            retry_transient_errors=retry_transient_errors,
        ))

    extra_body = (
        _omlx_prompt_extras(messages, reasoning_effort, preserve_thinking, model)
        if is_omlx else _listed_effort(reasoning_effort, reasoning_levels)
    )

    from llm.base import base_completion
    if is_omlx:
        from llm.providers.omlx_messages import shape_messages
        messages = shape_messages(messages, model, omlx_server_id)
    response = await _observed(api_base, base_completion(
        messages, tools,
        model=model, **output_budget,
        api_key=api_key, api_base=api_base, omlx_server_id=omlx_server_id,
        on_reasoning_chunk=on_reasoning_chunk,
        on_content_chunk=on_content_chunk,
        on_tool_call_chunk=on_tool_call_chunk, on_response_started=on_response_started,
        reasoning_effort=None,  # the level, if any, travels inside extra_body
        thread_id=thread_id,
        extra_body=extra_body,
        retry_transient_errors=retry_transient_errors,
    ))
    response.effort_sent = (extra_body or {}).get("reasoning_effort")
    return await mark_own_server(response, omlx_server_id)
