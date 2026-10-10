"""Exact-model, tool-free, sessionless structured inference transports."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, TypeVar, cast
from uuid import UUID

from loguru import logger

from connections.harness_process import killed_on_exit
from llm.isolated_failure import (
    nonnegative_cost as _nonnegative_cost,
    nonnegative_count as _nonnegative_count,
    provider_error_class,
)
from llm.isolated_contract import (
    DEFAULT_ISOLATED_TIMEOUT_SECONDS,
    IsolatedStructuredRequest,
    IsolatedStructuredTelemetry,
    IsolatedStructuredUsageCollector,
    StructuredDispatchObserver,
    StructuredRawResultObserver,
    TelemetrySink,
    _TelemetryAccumulator,
    prepare_isolated_structured_request,
)
from llm.resolution import ExecutionTarget
from subagents.codex_app_server import CodexAppServerSession
from subagents.cc_stream import STDOUT_LIMIT
from subagents.provider_usage_limits import is_provider_usage_or_quota_limit


T = TypeVar("T")
StructuredValidator = Callable[[object], T]
_CLAUDE_STRUCTURED_PAYLOAD_KEY = "payload"
_ISOLATED_RAW_RESULT_SCHEMA_VERSION = 1


def _is_canonical_uuid_text(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _optional_nonnegative_token(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _strict_json_loads(raw: str | bytes) -> Any:
    return json.loads(
        raw,
        object_pairs_hook=_strict_object,
        parse_constant=_reject_json_constant,
    )


def _claude_structured_usage(
    terminal: dict[str, Any],
    *,
    cache_miss: object | None,
) -> dict[str, Any]:
    raw = terminal.get("usage")
    if not isinstance(raw, dict):
        raw = {}
    input_tokens = _optional_nonnegative_token(raw.get("input_tokens"))
    output_tokens = _optional_nonnegative_token(raw.get("output_tokens"))
    cache_read = _optional_nonnegative_token(raw.get("cache_read_input_tokens"))
    cache_creation = _optional_nonnegative_token(
        raw.get("cache_creation_input_tokens")
    )
    total_input = (
        input_tokens + cache_read + cache_creation
        if None not in (input_tokens, cache_read, cache_creation)
        else None
    )
    processed = (
        total_input + output_tokens
        if total_input is not None and output_tokens is not None
        else None
    )
    usage = {
        "total_logical_input_tokens": total_input,
        "output_tokens": output_tokens,
        "processed_token_count": processed,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
        "invocation_usage": dict(raw),
    }
    explicit_cache_miss = terminal.get("cache_miss_reason") or cache_miss
    if explicit_cache_miss is not None:
        usage["cache_miss_reason"] = explicit_cache_miss
    return usage


class IsolatedStructuredError(RuntimeError):
    """One exact detached provider violated the structured-inference contract."""


class IsolatedStructuredRateLimitError(IsolatedStructuredError):
    """The exact configured provider refused the call due to rate limits."""


class IsolatedStructuredProviderError(IsolatedStructuredError):
    """The detached provider returned an unusable structured response."""

class IsolatedStructuredOutputTruncatedError(IsolatedStructuredError):
    """The detached provider ended before producing complete structured output."""


class IsolatedStructuredContextOverflowError(IsolatedStructuredError):
    """The exact serialized request exceeded the provider context window."""


class IsolatedStructuredUnavailableError(IsolatedStructuredError):
    """The exact configured provider or model is unavailable."""


class IsolatedStructuredIdentityError(IsolatedStructuredError):
    """The detached provider did not prove the configured model identity."""


class IsolatedStructuredTimeoutError(IsolatedStructuredError):
    """The detached provider exceeded its exact invocation deadline."""


def _capture_native_telemetry(
    envelope: dict[str, Any],
    request: "IsolatedStructuredRequest",
) -> None:
    observed = request._telemetry
    receipt = envelope.get("receipt")
    result = envelope.get("result")
    reported = receipt.get("reported_model") if isinstance(receipt, dict) else None
    observed.reported_model = reported if isinstance(reported, str) else None
    usage = result.get("usage") if isinstance(result, dict) else None
    if not isinstance(usage, dict):
        return
    token_fields = {
        "prompt_tokens": "prompt_tokens",
        "completion_tokens": "completion_tokens",
        "cache_creation_tokens": "cache_creation_tokens",
        "cache_read_tokens": "cache_read_tokens",
        "total_tokens": "total_tokens",
    }
    for source, destination in token_fields.items():
        if source in usage:
            setattr(observed, destination, _nonnegative_count(usage[source]))
    if any(getattr(observed, field) is not None for field in token_fields.values()):
        cost = _nonnegative_cost(usage.get("cost_usd"))
        # Native adapters use 0.0 both for free models and missing pricing.
        # Positive values alone prove that CORSIA pricing supplied this cost;
        # the host-review sink rechecks registry pricing for the zero-cost case.
        billable_fields = (
            "prompt_tokens",
            "completion_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
        )
        if (
            cost is not None
            and cost > 0
            and all(getattr(observed, field) is not None for field in billable_fields)
        ):
            observed.cost_usd = cost
            observed.cost_source = "corsia_pricing"


def _capture_claude_telemetry(
    envelope: dict[str, Any],
    target: ExecutionTarget,
    request: "IsolatedStructuredRequest",
) -> None:
    observed = request._telemetry
    model_usage = envelope.get("modelUsage")
    if isinstance(model_usage, dict):
        expected = target.expected_reported_model or target.transport_model
        if isinstance(model_usage.get(expected), dict):
            observed.reported_model = expected
        elif len(model_usage) == 1:
            sole_model, metadata = next(iter(model_usage.items()))
            if isinstance(sole_model, str) and isinstance(metadata, dict):
                observed.reported_model = sole_model

    usage = envelope.get("usage")
    if isinstance(usage, dict):
        token_fields = {
            "input_tokens": "prompt_tokens",
            "output_tokens": "completion_tokens",
            "cache_creation_input_tokens": "cache_creation_tokens",
            "cache_read_input_tokens": "cache_read_tokens",
            "total_tokens": "total_tokens",
        }
        for source, destination in token_fields.items():
            if source in usage:
                setattr(observed, destination, _nonnegative_count(usage[source]))
    cost = _nonnegative_cost(envelope.get("total_cost_usd"))
    if cost is not None:
        observed.cost_usd = cost
        observed.cost_source = "provider_reported"


def _capture_codex_usage(
    usage: dict[str, Any],
    observed: _TelemetryAccumulator | None,
) -> None:
    if observed is None:
        return
    totals = usage.get("total")
    if not isinstance(totals, dict):
        totals = usage.get("last")
    if not isinstance(totals, dict):
        return
    token_fields = {
        "inputTokens": "prompt_tokens",
        "outputTokens": "completion_tokens",
        "cacheCreationInputTokens": "cache_creation_tokens",
        "cachedInputTokens": "cache_read_tokens",
        "totalTokens": "total_tokens",
    }
    for source, destination in token_fields.items():
        if source in totals:
            setattr(observed, destination, _nonnegative_count(totals[source]))


async def _validate_native_target(target: ExecutionTarget) -> ExecutionTarget:
    from llm.resolution import validate_native_target

    return await validate_native_target(target)


def _make_provider_params(model: str) -> dict[str, Any]:
    from llm.capabilities import make_provider_params

    return make_provider_params(model)


async def _chat_completion(**kwargs):
    from llm import chat_completion

    return await chat_completion(**kwargs)


def _record_structured_usage(
    request: IsolatedStructuredRequest,
    usage: dict[str, Any] | None,
) -> None:
    if usage is not None and request.telemetry is not None:
        request.telemetry.usage_records.append(usage)


def _native_structured_usage_from_raw(
    raw: object,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw:
        return None
    invocation_usage = dict(raw)
    total_input = _optional_nonnegative_token(raw.get("effective_input_tokens"))
    if total_input is None:
        total_input = _optional_nonnegative_token(raw.get("prompt_tokens"))
    output_tokens = _optional_nonnegative_token(raw.get("completion_tokens"))
    cache_read = _optional_nonnegative_token(raw.get("cache_read_tokens"))
    cache_creation = _optional_nonnegative_token(raw.get("cache_creation_tokens"))
    processed = (
        total_input + output_tokens
        if total_input is not None and output_tokens is not None
        else None
    )
    final_occupancy = (
        {
            "input_tokens": total_input,
            "cached_input_tokens": cache_read,
        }
        if total_input is not None
        else None
    )
    return {
        "total_logical_input_tokens": total_input,
        "output_tokens": output_tokens,
        "processed_token_count": processed,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
        "cached_input_tokens": cache_read,
        "final_call_occupancy": final_occupancy,
        "invocation_usage": invocation_usage,
    }


def _raw_result_receipt(
    target: ExecutionTarget,
    *,
    reported_model: object,
) -> dict[str, Any]:
    frozen_reported = _strict_json_projection(reported_model)
    reported = frozen_reported.strip() if isinstance(frozen_reported, str) else ""
    return {
        "catalog_model": target.catalog_id,
        "harness": target.harness,
        "transport_model": target.transport_model,
        "reported_model": reported or None,
    }


def _strict_json_projection(value: object) -> object:
    """Freeze one provider field or retain only leak-safe invalid-shape evidence."""
    try:
        return json.loads(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    except Exception:
        evidence: dict[str, Any] = {
            "invalid_json_type": type(value).__name__,
        }
        if isinstance(value, (list, tuple, dict, set, frozenset)):
            try:
                evidence["container_size"] = min(len(value), 1_000_000)
            except Exception:
                evidence["container_size_unavailable"] = True
        return evidence


def _provider_attribute(value: object, name: str) -> object:
    try:
        return getattr(value, name)
    except Exception as exc:
        return {
            "invalid_attribute_error": type(exc).__name__,
            "attribute": name,
        }


def _json_safe_tool_calls(value: object) -> object:
    """Project native tool attempts to strict JSON without losing rejection proof."""
    if value is None:
        return None
    if not isinstance(value, list):
        return {
            "invalid_container_type": type(value).__name__,
            "present": True,
        }
    from llm.router import ToolCallRequest

    projected: list[dict[str, Any]] = []
    for call in value:
        if isinstance(call, dict):
            frozen = _strict_json_projection(call)
            projected.append(
                frozen
                if isinstance(frozen, dict)
                else {"invalid_json_type": type(frozen).__name__}
            )
            continue
        if not isinstance(call, ToolCallRequest):
            projected.append(
                {
                    "invalid_call_type": type(call).__name__,
                    "present": True,
                }
            )
            continue
        projected.append(
            {
                "id": _strict_json_projection(call.id),
                "name": _strict_json_projection(call.name),
                "arguments": _strict_json_projection(call.arguments),
                "source_type": type(call).__name__,
            }
        )
    return projected


def _native_raw_result_envelope(
    target: ExecutionTarget,
    response: object,
    *,
    provider_model: str,
) -> dict[str, Any]:
    return {
        "schema_version": _ISOLATED_RAW_RESULT_SCHEMA_VERSION,
        "receipt": _raw_result_receipt(
            target,
            reported_model=_provider_attribute(response, "reported_model"),
        ),
        "result": {
            "provider_model": provider_model,
            "finish_reason": _strict_json_projection(
                _provider_attribute(response, "finish_reason")
            ),
            "tool_calls": _json_safe_tool_calls(
                _provider_attribute(response, "tool_calls")
            ),
            "content": _strict_json_projection(
                _provider_attribute(response, "content")
            ),
            "reasoning_content": _strict_json_projection(
                _provider_attribute(response, "reasoning_content")
            ),
            "usage": _strict_json_projection(
                _provider_attribute(response, "usage")
            ),
        },
    }


def _materialize_native_raw_result(
    target: ExecutionTarget,
    request: IsolatedStructuredRequest,
    envelope: dict[str, Any],
) -> str:
    receipt = envelope.get("receipt")
    result = envelope.get("result")
    if (
        envelope.get("schema_version") != _ISOLATED_RAW_RESULT_SCHEMA_VERSION
        or not isinstance(receipt, dict)
        or receipt.get("harness") != "native"
        or receipt.get("catalog_model") != target.catalog_id
        or receipt.get("transport_model") != target.transport_model
        or not isinstance(result, dict)
    ):
        raise IsolatedStructuredError(
            "Native isolated inference raw-result envelope is invalid"
        )
    finish_reason = result.get("finish_reason")
    content = result.get("content")
    if request.accept_truncated_output:
        if not isinstance(content, str) or not content.strip():
            content = result.get("reasoning_content")
        if not isinstance(content, str) or not content.strip():
            raise IsolatedStructuredError(
                "Native isolated inference returned empty content"
            )
        return content
    if finish_reason != "stop":
        if finish_reason == "error" and _is_rate_limit_error_text(str(content or "")):
            raise IsolatedStructuredRateLimitError(
                "Native isolated inference was rate limited"
            )
        if finish_reason == "error":
            raise IsolatedStructuredProviderError(provider_error_class(content))
        if finish_reason == "length":
            raise IsolatedStructuredOutputTruncatedError("reviewer_output_truncated")
        raise IsolatedStructuredError(
            "Native isolated inference did not stop cleanly: "
            f"{finish_reason!r}"
        )
    if result.get("tool_calls"):
        raise IsolatedStructuredError("Native isolated inference attempted a tool")
    if request.require_reported_model:
        provider_model = str(result.get("provider_model") or "").strip()
        expected_model = (
            target.expected_reported_model or provider_model
        ).strip()
        reported_model = str(receipt.get("reported_model") or "").strip()
        expected_forms = {expected_model}
        if "/" in expected_model:
            expected_forms.add(expected_model.partition("/")[2])
        if not reported_model or reported_model not in expected_forms:
            raise IsolatedStructuredIdentityError(
                "Native isolated inference reported model mismatch: "
                f"{reported_model or '(missing)'!r} not in "
                f"{sorted(expected_forms)!r}"
            )
    if not isinstance(content, str) or not content.strip():
        raise IsolatedStructuredError(
            "Native isolated inference returned empty content"
        )
    return content


async def _invoke_native_structured(
    target: ExecutionTarget,
    request: IsolatedStructuredRequest,
    *,
    validate_target: Callable[[ExecutionTarget], Awaitable[ExecutionTarget]]
    | None = None,
    provider_params: Callable[[str], dict[str, Any]] | None = None,
    chat_completion: Callable[..., Awaitable[Any]] | None = None,
) -> object:
    """Run one stateless Native call with no session, callbacks, or tools."""
    if target.harness != "native":
        raise IsolatedStructuredError(
            "Native isolated inference received another harness"
        )
    await (validate_target or _validate_native_target)(target)
    provider = (provider_params or _make_provider_params)(target.transport_model)
    provider_model = str(provider.get("model") or target.transport_model).strip()
    completion_kwargs = {
        "messages": [
            {"role": "system", "content": request.system_prompt},
            *(
                [dict(message) for message in request.source_messages]
                or [{"role": "user", "content": request.user_prompt}]
            ),
        ],
        "tools": None,
        "model": provider_model,
        "max_tokens": request.max_output_tokens,
        "api_key": provider.get("api_key"),
        "api_base": provider.get("api_base"), "omlx_server_id": provider.get("omlx_server_id"),
        "on_reasoning_chunk": None,
        "on_content_chunk": None,
        "on_tool_call_chunk": None,
        "reasoning_effort": request.reasoning_effort,
        "thread_id": None,
        "preserve_thinking": None,
    }
    if not request.allow_provider_retries:
        completion_kwargs["retry_transient_errors"] = False
    try:
        if request.dispatch_observer is not None:
            await request.dispatch_observer()
        response = await asyncio.wait_for(
            (chat_completion or _chat_completion)(**completion_kwargs),
            timeout=request.timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise IsolatedStructuredTimeoutError(
            "Native isolated inference timed out"
        ) from exc
    except Exception as exc:
        if _is_context_overflow(exc):
            raise IsolatedStructuredContextOverflowError(
                "Native isolated inference exceeded the context window"
            ) from exc
        raise
    envelope = _native_raw_result_envelope(
        target,
        response,
        provider_model=provider_model,
    )
    raw_result = envelope.get("result")
    raw_usage = (
        raw_result.get("usage")
        if isinstance(raw_result, dict)
        else None
    )
    usage = _native_structured_usage_from_raw(raw_usage)
    _record_structured_usage(request, usage)
    if request.raw_result_observer is not None:
        await request.raw_result_observer(envelope, usage)
    _capture_native_telemetry(envelope, request)
    return _materialize_native_raw_result(target, request, envelope)


def _clean_claude_env() -> dict[str, str]:
    from subagents.cc_env import clean_claude_env

    return clean_claude_env()


def _is_rate_limit_error_text(error_text: str) -> bool:
    low = error_text.lower()
    return any(
        marker in low
        for marker in ("rate_limit", "rate limit", "too many requests", "429")
    )


def _bounded_error_descriptor(value: object) -> str:
    """Extract only bounded classification fields from one provider error."""
    if isinstance(value, str):
        return value[:1_000]
    if not isinstance(value, dict):
        return ""
    return " ".join(
        str(value[key])[:500]
        for key in ("code", "type", "message", "reason")
        if isinstance(value.get(key), (str, int))
        and not isinstance(value.get(key), bool)
    )[:1_000]


# Provider account-limit wording is shared with reviewer harnesses.


def _is_context_overflow(error: object) -> bool:
    """Recognize provider and harness spellings of one oversized request."""
    name = type(error).__name__.lower()
    text = str(error).lower()
    return "contextwindowexceeded" in name or any(
        marker in text
        for marker in (
            "maximum context length",
            "context length exceeded",
            "context_length_exceeded",
            "context window exceeded",
            "exceeds context window",
            "exceeded the context window",
            "prompt is too long",
            "input is too long",
            "too many input tokens",
        )
    )


def _classify_claude_error(error_text: str, detail: str) -> Exception:
    low = error_text.lower()
    if _is_context_overflow(low):
        return IsolatedStructuredContextOverflowError(detail)
    if is_provider_usage_or_quota_limit(low):
        return IsolatedStructuredUnavailableError(detail)
    if _is_rate_limit_error_text(low):
        return IsolatedStructuredRateLimitError(detail)
    if any(
        marker in low
        for marker in (
            "temporarily unavailable",
            "overloaded",
            "529",
            # A provider we cannot authenticate to cannot execute this fold,
            # which is the fallback's trigger condition (contract §5.3.4) — not
            # our defect. Codex returned this and it was recorded as one.
            "401 unauthorized",
            "model not found",
            "model_not_found",
            "model not available",
            "unknown model",
            "invalid model",
            "issue with the selected model",
            "no endpoints",
            "decommissioned",
            "deprecated",
            "delisted",
        )
    ):
        return IsolatedStructuredUnavailableError(detail)
    return IsolatedStructuredError(detail)


def _raise_claude_error(error_text: str, detail: str) -> None:
    raise _classify_claude_error(error_text, detail)


def _compact_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _wrap_claude_output_schema(output_schema: dict[str, Any]) -> dict[str, Any]:
    """Nest caller output under one CC transport field without changing it."""
    payload_schema = dict(output_schema)
    definitions = payload_schema.pop("$defs", None)
    wrapped: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {_CLAUDE_STRUCTURED_PAYLOAD_KEY: payload_schema},
        "required": [_CLAUDE_STRUCTURED_PAYLOAD_KEY],
    }
    if definitions is not None:
        # Pydantic emits root-relative #/$defs references. Hoisting preserves
        # those references after the caller schema is nested under payload.
        wrapped["$defs"] = definitions
    return wrapped


def _claude_structured_env(
    request: IsolatedStructuredRequest,
    *,
    clean_env: Callable[[], dict[str, str]] | None,
    claude_dir: str | None = None,
) -> dict[str, str]:
    from subagents.cc_env import apply_claude_config_dir

    env = (clean_env or _clean_claude_env)()
    env.pop("CORSIA_THREAD_ID", None)
    env["CLAUDE_CODE_DISABLE_BUNDLED_SKILLS"] = "1"
    env["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"] = "1"
    apply_claude_config_dir(env, claude_dir)
    if request.allow_provider_retries:
        env.pop("CLAUDE_CODE_MAX_RETRIES", None)
    else:
        env["CLAUDE_CODE_MAX_RETRIES"] = "0"
    return env


def _claude_schema_json(request: IsolatedStructuredRequest) -> str:
    return _compact_json(_wrap_claude_output_schema(request.output_schema))


def _claude_structured_payload(
    envelope: dict[str, Any],
    *,
    expected_model: str,
    require_reported_model: bool,
    require_structured: bool,
    operation: str,
    error_output: str = "",
) -> object:
    if envelope.get("is_error") or envelope.get("subtype") != "success":
        detail = str(envelope.get("result") or error_output or "provider error")
        _raise_claude_error(
            f"{detail} {error_output}",
            f"{operation} error envelope: {detail[:200]}",
        )
    if (  # A schema finalizer can report a third turn after two bounded model turns.
        type(envelope.get("num_turns")) is not int
        or not 1 <= envelope["num_turns"] <= 3
    ):
        raise IsolatedStructuredError(
            f"{operation} exceeded its bounded structured-output turn count"
        )
    model_usage = envelope.get("modelUsage")
    if not isinstance(model_usage, dict) or not isinstance(
        model_usage.get(expected_model), dict
    ):
        raise IsolatedStructuredIdentityError(
            f"{operation} did not report selected model {expected_model!r}"
        )
    if require_reported_model and set(model_usage) != {expected_model}:
        raise IsolatedStructuredIdentityError(
            f"{operation} reported an additional model: "
            f"{sorted(set(model_usage) - {expected_model})!r}"
        )
    if envelope.get("permission_denials"):
        raise IsolatedStructuredError(f"{operation} attempted a denied tool action")
    structured = envelope.get("structured_output")
    if structured is None and not require_structured:
        return envelope.get("result")
    if not isinstance(structured, dict) or set(structured) != {
        _CLAUDE_STRUCTURED_PAYLOAD_KEY
    }:
        raise IsolatedStructuredError(
            f"{operation} returned an invalid structured transport envelope"
        )
    return structured[_CLAUDE_STRUCTURED_PAYLOAD_KEY]


def _claude_raw_result_envelope(
    target: ExecutionTarget,
    *,
    stdout: str,
    stderr: str,
    returncode: int | None,
    parsed_envelope: object,
) -> dict[str, Any]:
    reported_model = None
    if isinstance(parsed_envelope, dict):
        model_usage = parsed_envelope.get("modelUsage")
        expected = target.expected_reported_model or target.transport_model
        if isinstance(model_usage, dict):
            if isinstance(model_usage.get(expected), dict):
                reported_model = expected
            elif len(model_usage) == 1:
                sole_model, metadata = next(iter(model_usage.items()))
                if isinstance(sole_model, str) and isinstance(metadata, dict):
                    reported_model = sole_model
    return {
        "schema_version": _ISOLATED_RAW_RESULT_SCHEMA_VERSION,
        "receipt": _raw_result_receipt(
            target,
            reported_model=reported_model,
        ),
        "result": {
            "stdout": stdout,
            "stderr": stderr,
            "returncode": returncode,
        },
    }


def _materialize_claude_raw_result(
    target: ExecutionTarget,
    request: IsolatedStructuredRequest,
    envelope: dict[str, Any],
) -> object:
    receipt = envelope.get("receipt")
    result = envelope.get("result")
    if (
        envelope.get("schema_version") != _ISOLATED_RAW_RESULT_SCHEMA_VERSION
        or not isinstance(receipt, dict)
        or receipt.get("harness") != "claude-code"
        or receipt.get("catalog_model") != target.catalog_id
        or receipt.get("transport_model") != target.transport_model
        or not isinstance(result, dict)
    ):
        raise IsolatedStructuredError(
            "Claude isolated inference raw-result envelope is invalid"
        )
    output = result.get("stdout")
    error_output = result.get("stderr")
    returncode = result.get("returncode")
    if not isinstance(output, str) or not isinstance(error_output, str):
        raise IsolatedStructuredError(
            "Claude isolated inference raw-result bytes are invalid"
        )
    parse_error: BaseException | None = None
    try:
        parsed = _strict_json_loads(output)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        parsed = None
        parse_error = exc
    if request.accept_truncated_output and isinstance(parsed, dict):
        output = parsed.get("structured_output")
        if isinstance(output, dict) and _CLAUDE_STRUCTURED_PAYLOAD_KEY in output:
            output = output[_CLAUDE_STRUCTURED_PAYLOAD_KEY]
        if output in (None, "", (), [], {}):
            output = parsed.get("result")
        if output not in (None, "", (), [], {}) and not (
            isinstance(output, str) and not output.strip()
        ):
            return output
    if returncode != 0:
        _raise_claude_error(
            f"{output} {error_output}",
            "Claude isolated inference failed "
            f"(rc={returncode}): {(error_output or output)[:200]}",
        )
    if parse_error is not None:
        raise IsolatedStructuredError(
            "Claude isolated inference emitted a non-JSON envelope"
        ) from parse_error
    if not isinstance(parsed, dict):
        raise IsolatedStructuredError(
            "Claude isolated inference envelope is not an object"
        )
    if request.accept_truncated_output:
        raise IsolatedStructuredError(
            "Claude isolated inference returned empty content"
        )
    expected_model = target.expected_reported_model or target.transport_model
    return _claude_structured_payload(
        parsed,
        expected_model=expected_model,
        require_reported_model=request.require_reported_model,
        require_structured=False,
        operation="Claude isolated inference",
        error_output=error_output,
    )


def _claude_structured_argv(
    target: ExecutionTarget,
    request: IsolatedStructuredRequest,
) -> list[str]:
    argv = [
        "-p", "--model", target.transport_model,
        "--system-prompt", request.system_prompt,
    ]
    if request.require_structured_output:
        argv.extend(["--json-schema", _claude_schema_json(request)])
    argv.extend([
        "--output-format", "json",
        "--no-session-persistence",
        "--safe-mode",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--permission-mode", "dontAsk",
        "--tools", "",
        "--disable-slash-commands",
        "--no-chrome",
        "--max-turns", "2" if request.require_structured_output else "1",
    ])
    if request.reasoning_effort is not None:
        argv.extend(["--effort", request.reasoning_effort])
    return argv


async def _invoke_claude_code_structured(
    target: ExecutionTarget,
    request: IsolatedStructuredRequest,
    *,
    binary: str,
    clean_env: Callable[[], dict[str, str]] | None = None,
) -> object:
    """Run one isolated Claude Code print-mode structured-output invocation."""
    if target.harness != "claude-code":
        raise IsolatedStructuredError(
            "Claude isolated inference received another harness"
        )
    if not target.available:
        raise IsolatedStructuredUnavailableError(
            f"Claude Code target is unavailable: "
            f"{target.availability_reason or 'unknown'}"
    )
    from llm.isolated_admission import admit_structured, require_structured_write
    from connections.account_runtime import LaunchHomes
    request = admit_structured(target, request)
    env = _claude_structured_env(request, clean_env=clean_env, claude_dir=cast(LaunchHomes, request.vendor_launch).claude_dir)
    with tempfile.TemporaryDirectory(prefix="corsia-isolated-cc-") as cwd:
        require_structured_write(target, request)
        process = await asyncio.create_subprocess_exec(
            binary,
            *_claude_structured_argv(target, request),
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STDOUT_LIMIT,
            env=env,
        )
        # A timed-out, failed or cancelled call kills the CLI with a bounded
        # reap: a descendant holding its pipes must not strand the caller.
        try:
            async with killed_on_exit(process):
                if request.dispatch_observer is not None:
                    await request.dispatch_observer()
                require_structured_write(target, request)
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(request.user_prompt.encode()),
                    timeout=request.timeout_seconds,
                )
        except asyncio.TimeoutError as exc:
            raise IsolatedStructuredTimeoutError(
                "Claude isolated inference timed out"
            ) from exc

    output = stdout.decode("utf-8", "replace").strip()
    error_output = stderr.decode("utf-8", "replace").strip()
    try:
        parsed_envelope = _strict_json_loads(output)
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed_envelope = None
    usage = (
        _claude_structured_usage(parsed_envelope, cache_miss=None)
        if isinstance(parsed_envelope, dict)
        and isinstance(parsed_envelope.get("usage"), dict)
        else None
    )
    _record_structured_usage(request, usage)
    if isinstance(parsed_envelope, dict):
        _capture_claude_telemetry(parsed_envelope, target, request)
    envelope = _claude_raw_result_envelope(
        target,
        stdout=output,
        stderr=error_output,
        returncode=process.returncode,
        parsed_envelope=parsed_envelope,
    )
    if request.raw_result_observer is not None:
        await request.raw_result_observer(envelope, usage)
    return _materialize_claude_raw_result(target, request, envelope)


_codex_session_factory = CodexAppServerSession

_CODEX_ALLOWED_EVENT_KINDS = frozenset(
    {
        "thread_started",
        "model_reported",
        "text_delta",
        "assistant_text",
        "step_reasoning",
        "result", "call_usage",  # a spent token reading: no ledger row here, ignored
    }
)

_CODEX_CONFIG_OVERRIDES = (
    "features.plugins=false",
    "include_permissions_instructions=false",
    "include_apps_instructions=false",
    "include_collaboration_mode_instructions=false",
    "include_environment_context=false",
)


@dataclass
class _CodexProtocolState:
    """Invocation-local truth used to validate every inbound app-server frame."""

    requested_model: str
    expected_reported_model: str
    working_dir: str
    output_schema: dict[str, Any]
    user_prompt: str
    developer_instructions: str
    allow_provider_retries: bool = True
    thread_id: str | None = None
    turn_id: str | None = None
    turn_started: bool = False
    terminal_seen: bool = False
    token_usage: dict[str, Any] | None = None
    response_ids: set[int] = field(default_factory=set)
    telemetry: _TelemetryAccumulator | None = None


def _require_protocol_id(
    value: object,
    expected: str | None,
    *,
    label: str,
) -> None:
    if not isinstance(expected, str) or value != expected:
        raise IsolatedStructuredError(
            f"Codex isolated inference {label} mismatch: {value!r} != {expected!r}"
        )


def _validate_codex_item(
    item: object,
    *,
    expected_user_prompt: str,
) -> None:
    if not isinstance(item, dict):
        raise IsolatedStructuredError(
            "Codex isolated inference emitted a malformed item"
        )
    item_type = item.get("type")
    if item_type == "agentMessage":
        if item.get("memoryCitation") is not None:
            raise IsolatedStructuredError(
                "Codex isolated inference agentMessage carried a non-null memoryCitation"
            )
        return
    if item_type == "reasoning":
        return
    if item_type == "userMessage":
        content = item.get("content")
        if not isinstance(content, list) or any(
            not isinstance(part, dict)
            or part.get("type") != "text"
            or not isinstance(part.get("text"), str)
            for part in content
        ):
            raise IsolatedStructuredError(
                "Codex isolated inference userMessage was not text-only"
            )
        if "".join(part["text"] for part in content) != expected_user_prompt:
            raise IsolatedStructuredError(
                "Codex isolated inference userMessage changed the quoted input"
            )
        return
    raise IsolatedStructuredError(
        f"Codex isolated inference observed forbidden item type: {item_type!r}"
    )


def _validate_codex_response(
    message: dict[str, Any],
    *,
    request_log: list[dict[str, Any]],
    state: _CodexProtocolState,
) -> None:
    request_id = message.get("id")
    if not isinstance(request_id, int) or isinstance(request_id, bool):
        raise IsolatedStructuredError(
            f"Codex isolated inference received invalid response id: {request_id!r}"
        )
    if request_id in state.response_ids:
        raise IsolatedStructuredError(
            f"Codex isolated inference received duplicate response id: {request_id}"
        )
    matches = [entry for entry in request_log if entry.get("id") == request_id]
    if len(matches) != 1:
        raise IsolatedStructuredError(
            f"Codex isolated inference received unknown response id: {request_id!r}"
        )
    request = matches[0]
    method = request.get("method")
    if method not in {"initialize", "thread/start", "turn/start"}:
        raise IsolatedStructuredError(
            f"Codex isolated inference received a response for unexpected method: {method!r}"
        )
    state.response_ids.add(request_id)
    if "error" in message:
        return
    result = message.get("result")
    if not isinstance(result, dict):
        raise IsolatedStructuredError(
            f"Codex isolated inference {method} response is not an object"
        )
    if method == "initialize":
        return

    params = request.get("params") or {}
    if params.get("model") != state.requested_model:
        raise IsolatedStructuredError(
            f"Codex isolated inference {method} request lost its exact model"
        )
    if params.get("environments") != []:
        raise IsolatedStructuredError(
            f"Codex isolated inference {method} request exposed environments"
        )
    if method == "thread/start":
        required = {
            "cwd": state.working_dir,
            "sandbox": "read-only",
            "approvalPolicy": "never",
            "ephemeral": True,
            "allowProviderModelFallback": False,
            "developerInstructions": state.developer_instructions,
        }
        for key, expected in required.items():
            if params.get(key) != expected:
                raise IsolatedStructuredError(
                    f"Codex isolated inference thread/start has invalid {key}"
                )
        if params.get("dynamicTools") not in (None, []):
            raise IsolatedStructuredError(
                "Codex isolated inference thread/start registered dynamic tools"
            )
        reported_model = result.get("model")
        if isinstance(reported_model, str) and state.telemetry is not None:
            state.telemetry.reported_model = reported_model
        if reported_model != state.expected_reported_model:
            raise IsolatedStructuredIdentityError(
                "Codex isolated inference thread/start response has invalid model"
            )
        checks = {
            "cwd": state.working_dir,
            "approvalPolicy": "never",
            "activePermissionProfile": None,
            "instructionSources": [],
        }
        for key, expected in checks.items():
            if result.get(key) != expected:
                raise IsolatedStructuredError(
                    f"Codex isolated inference thread/start response has invalid {key}"
                )
        sandbox = result.get("sandbox")
        if not isinstance(sandbox, dict) or sandbox.get("type") != "readOnly":
            raise IsolatedStructuredError(
                "Codex isolated inference thread/start response is not read-only"
            )
        if sandbox.get("networkAccess") not in (False, None):
            raise IsolatedStructuredError(
                "Codex isolated inference thread/start response exposed network access"
            )
        thread = result.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise IsolatedStructuredError(
                "Codex isolated inference thread/start response has no thread id"
            )
        if (
            thread.get("cwd") != state.working_dir
            or thread.get("ephemeral") is not True
        ):
            raise IsolatedStructuredError(
                "Codex isolated inference thread/start response lost isolated thread state"
            )
        if thread.get("turns") not in (None, []):
            raise IsolatedStructuredError(
                "Codex isolated inference thread/start response contained prior turns"
            )
        status = thread.get("status")
        if isinstance(status, dict) and status.get("type") != "idle":
            raise IsolatedStructuredError(
                "Codex isolated inference thread/start response was not idle"
            )
        if state.thread_id is not None and state.thread_id != thread["id"]:
            raise IsolatedStructuredError(
                "Codex isolated inference thread/start response changed thread id"
            )
        state.thread_id = thread["id"]
        return

    if params.get("threadId") != state.thread_id:
        raise IsolatedStructuredError(
            "Codex isolated inference turn/start request changed thread id"
        )
    if params.get("outputSchema") != state.output_schema:
        raise IsolatedStructuredError(
            "Codex isolated inference turn/start request changed outputSchema"
        )
    expected_input = [{"type": "text", "text": state.user_prompt, "text_elements": []}]
    if params.get("input") != expected_input:
        raise IsolatedStructuredError(
            "Codex isolated inference turn/start request changed quoted input"
        )
    turn = result.get("turn")
    if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
        raise IsolatedStructuredError(
            "Codex isolated inference turn/start response has no turn id"
        )
    if turn.get("status") != "inProgress" or turn.get("items") not in (None, []):
        raise IsolatedStructuredError(
            "Codex isolated inference turn/start response was not a fresh empty turn"
        )
    if state.turn_id is not None and state.turn_id != turn["id"]:
        raise IsolatedStructuredError(
            "Codex isolated inference turn/start response changed turn id"
        )
    state.turn_id = turn["id"]
    state.turn_started = True


def _validate_codex_status(params: dict[str, Any], state: _CodexProtocolState) -> None:
    _require_protocol_id(params.get("threadId"), state.thread_id, label="thread id")
    status = params.get("status")
    if not isinstance(status, dict):
        raise IsolatedStructuredError(
            "Codex isolated inference emitted malformed thread status"
        )
    status_type = status.get("type")
    if status_type == "active":
        flags = status.get("activeFlags") or []
        if flags:
            raise IsolatedStructuredError(
                f"Codex isolated inference thread is waiting on unsafe activity: {flags!r}"
            )
        return
    if status_type == "idle":
        return
    raise IsolatedStructuredError(
        f"Codex isolated inference observed unsafe thread status: {status_type!r}"
    )


def _validate_codex_settings(
    params: dict[str, Any],
    state: _CodexProtocolState,
) -> None:
    _require_protocol_id(params.get("threadId"), state.thread_id, label="thread id")
    settings = params.get("threadSettings")
    if not isinstance(settings, dict):
        raise IsolatedStructuredError(
            "Codex isolated inference emitted malformed settings"
        )
    checks = {
        "model": state.expected_reported_model,
        "cwd": state.working_dir,
        "approvalPolicy": "never",
        "activePermissionProfile": None,
    }
    settings_model = settings.get("model")
    if isinstance(settings_model, str) and state.telemetry is not None:
        state.telemetry.reported_model = settings_model
    for key, expected in checks.items():
        if settings.get(key) != expected:
            error = (
                IsolatedStructuredIdentityError
                if key == "model"
                else IsolatedStructuredError
            )
            raise error(f"Codex isolated inference settings changed {key}")
    sandbox = settings.get("sandboxPolicy")
    if not isinstance(sandbox, dict) or sandbox.get("type") != "readOnly":
        raise IsolatedStructuredError(
            "Codex isolated inference settings changed sandbox"
        )
    if sandbox.get("networkAccess") not in (False, None):
        raise IsolatedStructuredError(
            "Codex isolated inference settings enabled network"
        )
    if settings.get("instructionSources") not in (None, []):
        raise IsolatedStructuredError(
            "Codex isolated inference settings exposed instruction sources"
        )
    collaboration = settings.get("collaborationMode") or {}
    collaboration_settings = collaboration.get("settings") or {}
    reported_instructions = collaboration_settings.get(
        "developer_instructions"
    ) or collaboration_settings.get("developerInstructions")
    if reported_instructions not in (None, state.developer_instructions):
        raise IsolatedStructuredError(
            "Codex isolated inference settings changed developer instructions"
        )


def _validate_codex_turn_completed(
    params: dict[str, Any],
    state: _CodexProtocolState,
) -> None:
    _require_protocol_id(params.get("threadId"), state.thread_id, label="thread id")
    turn = params.get("turn")
    if not isinstance(turn, dict):
        raise IsolatedStructuredError(
            "Codex isolated inference emitted malformed terminal Turn"
        )
    _require_protocol_id(turn.get("id"), state.turn_id, label="turn id")
    if not state.turn_started:
        raise IsolatedStructuredError(
            "Codex isolated inference completed an unstarted Turn"
        )
    if state.terminal_seen:
        raise IsolatedStructuredError(
            "Codex isolated inference emitted duplicate terminal"
        )
    if turn.get("status") != "completed":
        raise IsolatedStructuredError(
            f"Codex isolated inference terminal was {turn.get('status')!r}"
        )
    items = turn.get("items") or []
    if not isinstance(items, list):
        raise IsolatedStructuredError(
            "Codex isolated inference terminal items are malformed"
        )
    for item in items:
        _validate_codex_item(
            item,
            expected_user_prompt=state.user_prompt,
        )
    state.terminal_seen = True


def _validate_codex_progress_notification(
    method: str,
    params: dict[str, Any],
    state: _CodexProtocolState,
) -> None:
    """Validate inert streaming telemetry against the exact detached Turn."""
    _require_protocol_id(params.get("threadId"), state.thread_id, label="thread id")
    _require_protocol_id(params.get("turnId"), state.turn_id, label="turn id")
    if method == "thread/tokenUsage/updated":
        if set(params) != {"threadId", "turnId", "tokenUsage"} or not isinstance(
            params.get("tokenUsage"), dict
        ):
            raise IsolatedStructuredError(
                "Codex isolated inference emitted malformed token-usage telemetry"
            )
        try:
            frozen_usage = _strict_json_loads(
                json.dumps(
                    params["tokenUsage"],
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IsolatedStructuredError(
                "Codex isolated inference emitted non-JSON token usage"
            ) from exc
        if not isinstance(frozen_usage, dict):  # pragma: no cover - shape above.
            raise IsolatedStructuredError(
                "Codex isolated inference emitted malformed token usage"
            )
        state.token_usage = frozen_usage
        _capture_codex_usage(params["tokenUsage"], state.telemetry)
        return

    expected_keys = {"threadId", "turnId", "itemId"}
    if not isinstance(params.get("itemId"), str):
        raise IsolatedStructuredError(
            "Codex isolated inference progress notification lacked an item id"
        )
    if method == "item/reasoning/summaryPartAdded":
        expected_keys.add("summaryIndex")
        summary_index = params.get("summaryIndex")
        if isinstance(summary_index, bool) or not isinstance(summary_index, int):
            raise IsolatedStructuredError(
                "Codex isolated inference reasoning summary index is malformed"
            )
    else:
        expected_keys.add("delta")
        if not isinstance(params.get("delta"), str):
            raise IsolatedStructuredError(
                "Codex isolated inference progress delta is malformed"
            )
        if method == "item/reasoning/summaryTextDelta":
            expected_keys.add("summaryIndex")
            summary_index = params.get("summaryIndex")
            if isinstance(summary_index, bool) or not isinstance(summary_index, int):
                raise IsolatedStructuredError(
                    "Codex isolated inference reasoning summary index is malformed"
                )
        elif method == "item/reasoning/textDelta":
            expected_keys.add("contentIndex")
            content_index = params.get("contentIndex")
            if isinstance(content_index, bool) or not isinstance(content_index, int):
                raise IsolatedStructuredError(
                    "Codex isolated inference reasoning content index is malformed"
                )
    if set(params) != expected_keys:
        raise IsolatedStructuredError(
            f"Codex isolated inference {method} telemetry shape changed"
        )


def _validate_raw_codex_message(
    message: dict[str, Any],
    *,
    request_log: list[dict[str, Any]],
    state: _CodexProtocolState,
) -> None:
    """Reject any inbound app-server operation outside detached inference."""
    if not isinstance(message, dict):
        raise IsolatedStructuredError("Codex app-server emitted a non-object message")
    method = message.get("method")
    if method is None:
        if "id" in message and ("result" in message or "error" in message):
            _validate_codex_response(
                message,
                request_log=request_log,
                state=state,
            )
            return
        raise IsolatedStructuredError("Codex app-server emitted an unknown response")
    if "id" in message:
        raise IsolatedStructuredError(
            f"Codex isolated inference received a server tool/request method: {method}"
        )
    params = message.get("params") or {}
    if not isinstance(params, dict):
        raise IsolatedStructuredError(
            f"Codex isolated inference {method} parameters are not an object"
        )
    if method in {"item/started", "item/completed"}:
        _require_protocol_id(params.get("threadId"), state.thread_id, label="thread id")
        _require_protocol_id(params.get("turnId"), state.turn_id, label="turn id")
        item = params.get("item") or {}
        _validate_codex_item(
            item,
            expected_user_prompt=state.user_prompt,
        )
        return
    if method == "model/rerouted":
        rerouted = params.get("toModel") or params.get("model")
        if isinstance(rerouted, str) and state.telemetry is not None:
            state.telemetry.reported_model = rerouted
        raise IsolatedStructuredIdentityError(
            "Codex isolated inference observed model/rerouted"
        )
    if method == "thread/status/changed":
        _validate_codex_status(params, state)
        return
    if method == "thread/settings/updated":
        _validate_codex_settings(params, state)
        return
    if method == "turn/completed":
        _validate_codex_turn_completed(params, state)
        return
    if method == "thread/started":
        thread = params.get("thread") or {}
        thread_id = thread.get("id") or params.get("threadId")
        if not isinstance(thread_id, str):
            raise IsolatedStructuredError(
                "Codex isolated inference thread/started lacked id"
            )
        if state.thread_id is not None and state.thread_id != thread_id:
            raise IsolatedStructuredError(
                "Codex isolated inference thread/started changed id"
            )
        state.thread_id = thread_id
        return
    if method == "turn/started":
        _require_protocol_id(params.get("threadId"), state.thread_id, label="thread id")
        turn = params.get("turn") or {}
        turn_id = turn.get("id") or params.get("turnId")
        if not isinstance(turn_id, str):
            raise IsolatedStructuredError(
                "Codex isolated inference turn/started lacked id"
            )
        if state.turn_id is not None and state.turn_id != turn_id:
            raise IsolatedStructuredError(
                "Codex isolated inference turn/started changed id"
            )
        items = turn.get("items") or []
        if not isinstance(items, list):
            raise IsolatedStructuredError(
                "Codex isolated inference turn/started items malformed"
            )
        for item in items:
            _validate_codex_item(
                item,
                expected_user_prompt=state.user_prompt,
            )
        state.turn_id = turn_id
        state.turn_started = True
        return
    if method == "remoteControl/status/changed":
        if params.get("status") != "disabled":
            raise IsolatedStructuredError(
                "Codex isolated inference remote control is enabled"
            )
        return
    if method == "error":
        _require_protocol_id(params.get("threadId"), state.thread_id, label="thread id")
        _require_protocol_id(params.get("turnId"), state.turn_id, label="turn id")
        will_retry = params.get("willRetry")
        if _is_context_overflow(params.get("error") or params):
            raise IsolatedStructuredContextOverflowError(
                "Codex isolated inference exceeded the context window"
            )
        if will_retry is True and state.allow_provider_retries:
            return
        if will_retry is True:
            raise IsolatedStructuredError(
                "Codex isolated inference received forbidden provider retry"
            )
        if _is_rate_limit_error_text(
            _bounded_error_descriptor(params.get("error", params))
        ):
            raise IsolatedStructuredRateLimitError(
                "Codex isolated inference was rate limited"
            )
        raise IsolatedStructuredError(
            "Codex isolated inference received terminal error"
        )
    if method in {
        "thread/tokenUsage/updated",
        "item/agentMessage/delta",
        "item/reasoning/summaryPartAdded",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/textDelta",
    }:
        _validate_codex_progress_notification(method, params, state)
        return
    if method == "configWarning":
        summary = params.get("summary")
        if (
            not isinstance(summary, str)
            or "disabled" not in summary.lower()
            or "until the project is trusted" not in summary.lower()
            or not set(params).issubset({"summary", "details", "path", "range"})
        ):
            raise IsolatedStructuredError(
                "Codex isolated inference configWarning was unsafe"
            )
        return
    if method == "account/rateLimits/updated":
        if set(params) != {"rateLimits"} or not isinstance(
            params.get("rateLimits"), dict
        ):
            raise IsolatedStructuredError(
                "Codex isolated inference emitted malformed rate-limit telemetry"
            )
        return
    _note_unknown_codex_notification(method)


_UNKNOWN_NOTIFICATION_METHODS_SEEN: set[str] = set()
_UNKNOWN_NOTIFICATION_METHODS_CAP = 32


def _note_unknown_codex_notification(method: str) -> None:
    """Log one warning per unknown non-essential notification, then ignore it.

    Unknown Codex *requests* (messages carrying an ``id``) still fail loud
    above: they demand a response and may carry tool or permission semantics.
    An unknown *notification* is fire-and-forget telemetry our protocol state
    machine never consumes (e.g. ``turn/moderationMetadata``), so rejecting it
    would kill every in-flight inference on this transport whenever the Codex
    CLI adds a new telemetry stream. Known notifications keep their strict
    shape validation.

    The dedupe set is keyed on externally-sourced strings, so it is capped:
    once full, further unknown methods are still ignored, just not logged
    individually.
    """
    if method in _UNKNOWN_NOTIFICATION_METHODS_SEEN:
        return
    if len(_UNKNOWN_NOTIFICATION_METHODS_SEEN) >= _UNKNOWN_NOTIFICATION_METHODS_CAP:
        return
    _UNKNOWN_NOTIFICATION_METHODS_SEEN.add(method)
    logger.warning(
        "Codex isolated inference ignoring unknown non-essential "
        "notification {!r}; the codex protocol surface has drifted",
        method,
    )
    if len(_UNKNOWN_NOTIFICATION_METHODS_SEEN) == _UNKNOWN_NOTIFICATION_METHODS_CAP:
        logger.warning(
            "Codex isolated inference reached {} distinct unknown "
            "notification methods; further ones are ignored without logging",
            _UNKNOWN_NOTIFICATION_METHODS_CAP,
        )


def _new_terminal_future() -> asyncio.Future[None]:
    return asyncio.get_running_loop().create_future()


def _codex_structured_usage(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    total = raw.get("total")
    last = raw.get("last")
    if not isinstance(total, dict) or not isinstance(last, dict):
        return None
    total_input = _optional_nonnegative_token(total.get("inputTokens"))
    output_tokens = _optional_nonnegative_token(total.get("outputTokens"))
    cached_input = _optional_nonnegative_token(total.get("cachedInputTokens"))
    processed = (
        total_input + output_tokens
        if total_input is not None and output_tokens is not None
        else None
    )
    final_input = _optional_nonnegative_token(last.get("inputTokens"))
    final_cached = _optional_nonnegative_token(last.get("cachedInputTokens"))
    final_occupancy = None
    if final_input is not None:
        final_occupancy = {
            "input_tokens": final_input,
            "cached_input_tokens": final_cached,
        }
        context_window = _optional_nonnegative_token(raw.get("modelContextWindow"))
        if context_window is not None and context_window > 0:
            final_occupancy["model_context_window"] = context_window
    return {
        "total_logical_input_tokens": total_input,
        "output_tokens": output_tokens,
        "processed_token_count": processed,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
        "cached_input_tokens": cached_input,
        "final_call_occupancy": final_occupancy,
        "invocation_usage": dict(raw),
    }


def _consume_terminal_future(terminal: asyncio.Future[None]) -> None:
    """Cancel an unused waiter or retrieve its terminal exception."""
    if not terminal.done():
        terminal.cancel()
    elif not terminal.cancelled():
        terminal.exception()


def _freeze_raw_json(value: object, *, label: str) -> Any:
    try:
        return _strict_json_loads(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise IsolatedStructuredError(
            f"{label} was not strict JSON"
        ) from exc


def _codex_usage_from_raw_messages(
    raw_messages: list[dict[str, Any]],
) -> dict[str, Any] | None:
    latest = None
    for message in raw_messages:
        if message.get("method") != "thread/tokenUsage/updated":
            continue
        params = message.get("params")
        if isinstance(params, dict) and isinstance(params.get("tokenUsage"), dict):
            latest = _freeze_raw_json(
                params["tokenUsage"],
                label="Codex isolated inference token usage",
            )
    return latest if isinstance(latest, dict) else None


def _codex_raw_result_envelope(
    target: ExecutionTarget,
    *,
    working_dir: str,
    request_log: list[dict[str, Any]],
    raw_messages: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    reported_model = None
    for event in events:
        if event.get("kind") == "model_reported":
            reported_model = event.get("model")
    return {
        "schema_version": _ISOLATED_RAW_RESULT_SCHEMA_VERSION,
        "receipt": _raw_result_receipt(
            target,
            reported_model=reported_model,
        ),
        "result": {
            "working_dir": working_dir,
            "request_log": _freeze_raw_json(
                request_log,
                label="Codex isolated inference request log",
            ),
            "raw_messages": _freeze_raw_json(
                raw_messages,
                label="Codex isolated inference raw messages",
            ),
            "events": _freeze_raw_json(
                events,
                label="Codex isolated inference events",
            ),
        },
    }


def _validate_codex_raw_protocol(
    target: ExecutionTarget,
    request: IsolatedStructuredRequest,
    *,
    working_dir: str,
    request_log: list[dict[str, Any]],
    raw_messages: list[dict[str, Any]],
) -> _CodexProtocolState:
    """Validate captured Codex frames without requiring a terminal result."""
    expected_model = target.expected_reported_model or target.transport_model
    state = _CodexProtocolState(
        requested_model=target.transport_model,
        expected_reported_model=expected_model,
        working_dir=working_dir,
        output_schema=request.output_schema,
        user_prompt=request.user_prompt,
        developer_instructions=request.system_prompt,
        allow_provider_retries=request.allow_provider_retries,
        telemetry=request._telemetry,
    )
    for message in raw_messages:
        _validate_raw_codex_message(
            message,
            request_log=request_log,
            state=state,
        )
    return state


def _codex_raw_result_contents(
    target: ExecutionTarget,
    envelope: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    receipt = envelope.get("receipt")
    result = envelope.get("result")
    if (
        envelope.get("schema_version") != _ISOLATED_RAW_RESULT_SCHEMA_VERSION
        or not isinstance(receipt, dict)
        or receipt.get("harness") != "codex"
        or receipt.get("catalog_model") != target.catalog_id
        or receipt.get("transport_model") != target.transport_model
        or not isinstance(result, dict)
    ):
        raise IsolatedStructuredError(
            "Codex isolated inference raw-result envelope is invalid"
        )
    working_dir = result.get("working_dir")
    request_log = result.get("request_log")
    raw_messages = result.get("raw_messages")
    events = result.get("events")
    if (
        not isinstance(working_dir, str)
        or not isinstance(request_log, list)
        or not isinstance(raw_messages, list)
        or not isinstance(events, list)
        or any(not isinstance(item, dict) for item in request_log)
        or any(not isinstance(item, dict) for item in raw_messages)
        or any(not isinstance(item, dict) for item in events)
    ):
        raise IsolatedStructuredError(
            "Codex isolated inference raw-result contents are invalid"
        )
    return working_dir, request_log, raw_messages, events


def _checkpoint_codex_output(events: list[dict[str, Any]]) -> str:
    assistant_results = [
        event.get("text")
        for event in events
        if event.get("kind") == "assistant_text"
        and isinstance(event.get("text"), str)
        and event["text"].strip()
    ]
    if assistant_results:
        return assistant_results[-1]
    if any(
        event.get("kind") == "turn_failed"
        and _is_context_overflow(event.get("message") or event)
        for event in events
    ):
        raise IsolatedStructuredContextOverflowError(
            "Codex isolated inference exceeded the context window"
        )
    raise IsolatedStructuredError("Codex isolated inference returned empty content")


def _strict_codex_assistant_results(events: list[dict[str, Any]]) -> list[str]:
    results: list[str] = []
    for event in events:
        if event.get("kind") != "assistant_text":
            continue
        text = event.get("text")
        if not isinstance(text, str) or not text.strip():
            raise IsolatedStructuredError(
                "Codex isolated inference returned empty assistant output"
            )
        results.append(text)
    if len(results) != 1:
        raise IsolatedStructuredError(
            "Codex isolated inference did not return exactly one assistant result"
        )
    return results


def _validate_strict_codex_events(
    target: ExecutionTarget,
    request: IsolatedStructuredRequest,
    events: list[dict[str, Any]],
) -> tuple[bool, bool]:
    expected_model = target.expected_reported_model or target.transport_model
    model_reported_seen = False
    terminal_result_seen = False
    for event in events:
        kind = event.get("kind")
        if kind == "turn_failed" and _is_context_overflow(
            event.get("message") or event
        ):
            raise IsolatedStructuredContextOverflowError(
                "Codex isolated inference exceeded the context window"
            )
        if kind not in _CODEX_ALLOWED_EVENT_KINDS:
            raise IsolatedStructuredError(
                f"Codex isolated inference observed forbidden event: {kind!r}"
            )
        if kind == "model_reported":
            reported = str(event.get("model") or "")
            request._telemetry.reported_model = reported or None
            if request.require_reported_model and reported != expected_model:
                raise IsolatedStructuredIdentityError(
                    "Codex isolated inference model rerouted from "
                    f"{expected_model!r} to {reported!r}"
                )
            model_reported_seen = True
        elif kind == "result":
            stop_reason = event.get("stop_reason")
            if event.get("is_error") or stop_reason not in {"completed", "stop"}:
                raise IsolatedStructuredError(
                    "Codex isolated inference did not complete cleanly: "
                    f"{stop_reason!r}"
                )
            terminal_result_seen = True
    return model_reported_seen, terminal_result_seen


def _materialize_codex_raw_result(
    target: ExecutionTarget,
    request: IsolatedStructuredRequest,
    envelope: dict[str, Any],
) -> str:
    working_dir, request_log, raw_messages, events = _codex_raw_result_contents(
        target,
        envelope,
    )
    if request.accept_truncated_output:
        return _checkpoint_codex_output(events)
    state = _validate_codex_raw_protocol(
        target,
        request,
        working_dir=working_dir,
        request_log=request_log,
        raw_messages=raw_messages,
    )
    model_reported_seen, terminal_result_seen = _validate_strict_codex_events(
        target,
        request,
        events,
    )
    if not terminal_result_seen:
        raise IsolatedStructuredError(
            "Codex isolated inference did not observe a terminal result"
        )
    assistant_results = _strict_codex_assistant_results(events)
    if request.require_raw_terminal and not state.terminal_seen:
        raise IsolatedStructuredError(
            "Codex isolated inference did not observe a raw terminal"
        )
    if request.require_reported_model and not model_reported_seen:
        raise IsolatedStructuredIdentityError(
            "Codex isolated inference did not report the selected model"
        )
    return assistant_results[0]


@dataclass
class _CodexInvocationCapture:
    request: IsolatedStructuredRequest
    terminal: asyncio.Future[None]
    raw_messages: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    live_state: _CodexProtocolState | None = None
    session: Any | None = None
    session_closed: bool = False
    usage_recorded: bool = False

    async def on_raw_message(self, message: dict[str, Any]) -> None:
        frozen = _freeze_raw_json(
            message,
            label="Codex isolated inference raw message",
        )
        if not isinstance(frozen, dict):
            raise IsolatedStructuredError(
                "Codex isolated inference raw message is not an object"
            )
        self.raw_messages.append(frozen)
        if self.live_state is not None:
            _validate_raw_codex_message(
                frozen,
                request_log=list(getattr(self.session, "request_log", ())),
                state=self.live_state,
            )

    async def on_event(self, event: dict[str, Any]) -> None:
        frozen = _freeze_raw_json(
            {key: value for key, value in event.items() if key != "token_volume"},
            label="Codex isolated event",  # token_volume is typed ledger-only.
        )
        if not isinstance(frozen, dict):
            raise IsolatedStructuredError(
                "Codex isolated inference event is not an object"
            )
        self.events.append(frozen)
        kind = frozen.get("kind")
        if self.live_state is not None and kind not in _CODEX_ALLOWED_EVENT_KINDS:
            raise IsolatedStructuredError(
                f"Codex isolated inference observed forbidden event: {kind!r}"
            )
        if kind in {"result", "turn_failed"} and not self.terminal.done():
            self.terminal.set_result(None)

    async def on_exit(self, returncode: int | None, stderr: str) -> None:
        if not self.terminal.done():
            self.terminal.set_exception(
                IsolatedStructuredError(
                    "Codex isolated inference app-server exited before completion "
                    f"(rc={returncode}): {stderr[:200]}"
                )
            )

    async def close(self, primary_error: BaseException | None) -> None:
        try:
            if not self.session_closed:
                await self.session.close()
        except BaseException as close_error:
            if primary_error is not None:
                primary_error.add_note(
                    f"Codex isolated inference close failed: {close_error}"
                )
                return
            raise IsolatedStructuredError(
                f"Codex isolated inference close failed: {close_error}"
            ) from close_error


def _new_codex_invocation_session(
    target: ExecutionTarget, request: IsolatedStructuredRequest, capture: _CodexInvocationCapture, *,
    binary: str, working_dir: Path, codex_home: Path, session_factory: Callable[..., Any] | None,
) -> Any:
    from llm.isolated_admission import new_codex_invocation_session

    return new_codex_invocation_session(target, request, capture, binary=binary,
        working_dir=working_dir, codex_home=codex_home,
        session_factory=session_factory or _codex_session_factory,
        config_overrides=_CODEX_CONFIG_OVERRIDES)


def _record_codex_invocation_usage(capture: _CodexInvocationCapture) -> dict[str, Any] | None:
    token_usage = _codex_usage_from_raw_messages(capture.raw_messages)
    usage = _codex_structured_usage(token_usage)
    _record_structured_usage(capture.request, usage)
    capture.usage_recorded = True
    return usage


async def _complete_codex_invocation(
    target: ExecutionTarget,
    request: IsolatedStructuredRequest,
    capture: _CodexInvocationCapture,
    working_dir: Path,
) -> str:
    session = capture.session
    if not await session.start():
        raise IsolatedStructuredError(
            "Codex isolated inference thread did not start"
        )
    if request.dispatch_observer is not None:
        await request.dispatch_observer()
    if not await session.start_turn(
        request.user_prompt,
        output_schema=request.output_schema,
    ):
        raise IsolatedStructuredError(
            "Codex isolated inference turn did not start"
        )
    await asyncio.wait_for(
        asyncio.shield(capture.terminal),
        timeout=request.timeout_seconds,
    )
    await session.close()
    capture.session_closed = True
    usage = _record_codex_invocation_usage(capture)
    envelope = _codex_raw_result_envelope(
        target,
        working_dir=str(working_dir),
        request_log=list(getattr(session, "request_log", ())),
        raw_messages=capture.raw_messages,
        events=capture.events,
    )
    if request.raw_result_observer is not None:
        await request.raw_result_observer(envelope, usage)
    return _materialize_codex_raw_result(target, request, envelope)


def _codex_failure_error(
    target: ExecutionTarget,
    capture: _CodexInvocationCapture,
    working_dir: Path,
    error: BaseException,
) -> BaseException:
    if capture.live_state is None:
        return error
    try:
        _validate_codex_raw_protocol(
            target,
            capture.request,
            working_dir=str(working_dir),
            request_log=list(getattr(capture.session, "request_log", ())),
            raw_messages=capture.raw_messages,
        )
    except IsolatedStructuredError as protocol_error:
        return protocol_error
    return error


async def _run_codex_invocation(
    target: ExecutionTarget,
    request: IsolatedStructuredRequest,
    capture: _CodexInvocationCapture,
    working_dir: Path,
) -> str:
    primary_error: BaseException | None = None
    try:
        return await _complete_codex_invocation(
            target,
            request,
            capture,
            working_dir,
        )
    except asyncio.TimeoutError as exc:
        primary_error = exc
        raise IsolatedStructuredTimeoutError(
            "Codex isolated inference call timed out"
        ) from exc
    except BaseException as exc:
        if not capture.usage_recorded:
            _record_codex_invocation_usage(capture)
        failure = _codex_failure_error(target, capture, working_dir, exc)
        primary_error = failure
        if failure is not exc:
            raise failure from exc
        raise
    finally:
        try:
            await capture.close(primary_error)
        finally:
            _consume_terminal_future(capture.terminal)


async def _invoke_codex_structured(
    target: ExecutionTarget,
    request: IsolatedStructuredRequest,
    *,
    binary: str,
    session_factory: Callable[..., Any] | None = None,
    terminal_factory: Callable[[], asyncio.Future[None]] | None = None,
) -> object:
    """Run one isolated Codex app-server thread through the shared transport."""
    if target.harness != "codex":
        raise IsolatedStructuredError(
            "Codex isolated inference adapter received another harness"
        )
    if not target.available:
        raise IsolatedStructuredError(
            f"Codex target is unavailable: {target.availability_reason or 'unknown'}"
        )
    from llm.isolated_admission import admit_structured

    request = admit_structured(target, request)
    terminal = (terminal_factory or _new_terminal_future)()
    capture = _CodexInvocationCapture(request=request, terminal=terminal)
    from connections.account_operation import operation_directory

    with operation_directory(request, prefix='corsia-isolated-codex-', kind='detached',
                             home_subdir='home', model=target.catalog_id) as (request, root):
        working_dir = root / 'cwd'
        codex_home = root / 'home'
        working_dir.mkdir()
        capture.request = request
        if not request.accept_truncated_output:
            capture.live_state = _validate_codex_raw_protocol(
                target,
                request,
                working_dir=str(working_dir),
                request_log=[],
                raw_messages=[],
            )
        capture.session = _new_codex_invocation_session(
            target,
            request,
            capture,
            binary=binary,
            working_dir=working_dir,
            codex_home=codex_home,
            session_factory=session_factory,
        )
        return await _run_codex_invocation(
            target,
            request,
            capture,
            working_dir,
        )


async def invoke_isolated_structured(
    target: ExecutionTarget,
    *,
    system_prompt: str,
    user_prompt: str,
    output_schema: dict[str, Any],
    validator: StructuredValidator[T],
    max_output_tokens: int,
    reasoning_effort: str | None = None,
    timeout_seconds: float = DEFAULT_ISOLATED_TIMEOUT_SECONDS,
    binary: str | None = None,
    telemetry_sink: TelemetrySink | None = None,
    thread_id: str | None = None,
    actor_thread_id: str | None = None,
    account_id: str | None = None,
    account_bound: bool = False,
) -> T:
    """Run one fresh exact-model structured call and validate its sole result.

    This seam owns transport isolation only. The caller owns policy text,
    schema, result validation, retry policy, and every surrounding lifecycle.
    """
    if not target.available:
        raise IsolatedStructuredUnavailableError(
            f"isolated target {target.catalog_id!r} is unavailable: "
            f"{target.availability_reason or 'unknown'}"
        )
    started_ns = time.monotonic_ns()
    request, telemetry = prepare_isolated_structured_request(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        output_schema=output_schema,
        max_output_tokens=max_output_tokens,
        reasoning_effort=reasoning_effort,
        timeout_seconds=timeout_seconds,
        thread_id=thread_id,
        actor_thread_id=actor_thread_id,
        account_id=account_id,
        account_bound=account_bound,
    )

    try:
        if target.harness == "native":
            raw = await _invoke_native_structured(target, request)
        else:
            exact_binary = str(binary or "").strip()
            if not exact_binary:
                raise IsolatedStructuredError(
                    f"isolated target {target.catalog_id!r} requires its exact binary"
                )
            if target.harness == "claude-code":
                raw = await _invoke_claude_code_structured(
                    target,
                    request,
                    binary=exact_binary,
                )
            elif target.harness == "codex":
                raw = await _invoke_codex_structured(
                    target,
                    request,
                    binary=exact_binary,
                )
            else:  # pragma: no cover - ExecutionTarget.harness is a closed Literal.
                raise IsolatedStructuredError(
                    f"unsupported isolated harness: {target.harness!r}"
                )
        return validator(raw)
    finally:
        if telemetry_sink is not None:
            elapsed_ms = max(1, (time.monotonic_ns() - started_ns) // 1_000_000)
            try:
                telemetry_sink(
                    telemetry.snapshot(target, elapsed_ms=elapsed_ms)
                )
            except Exception as exc:
                logger.warning(
                    "isolated structured telemetry sink failed for {}: {}",
                    target.catalog_id,
                    type(exc).__name__,
                )
