"""Codex app-server notification normalization helpers."""

from __future__ import annotations

import json
import logging
import math
from typing import Any

log = logging.getLogger(__name__)


_PASSIVE_ITEM_TYPES = {
    "userMessage",
    "agentMessage",
    "reasoning",
    "enteredReviewMode",
    "exitedReviewMode",
}
_COMPACTION_ITEM_TYPES = {
    "compaction", "compact", "summary", "compaction_trigger",
    "context_compaction", "contextCompaction",
}
_NATIVE_ACTION_TYPES = {
    "commandExecution",
    "plan",
    "fileChange",
    "mcpToolCall",
    "webSearch",
    "appCall",
    "hookExecution",
    "imageView",
    "imageGeneration",
    "sleep",
    "subagentCall",
    "collabAgentToolCall",
    "subAgentActivity",
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "execCommandApproval",
    "applyPatchApproval",
}


def to_dynamic_tool_specs(
    definitions: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flatten OpenAI tool definitions to Codex ``DynamicToolSpec`` shape.

    CORSIA's registry nests name, description, and parameters under
    ``function``. Codex expects those fields flat and calls the parameters
    ``inputSchema``; without this projection it rejects the entire tool surface.
    """
    specs: list[dict[str, Any]] = []
    for definition in definitions:
        function = (
            definition.get("function") if isinstance(definition, dict) else None
        )
        if function is None:
            specs.append(dict(definition))
            continue
        name = function.get("name")
        if not name:
            log.warning("skipping dynamic tool spec with no name: %r", definition)
            continue
        specs.append(
            {
                "type": definition.get("type", "function"),
                "name": name,
                "description": function.get("description", ""),
                "inputSchema": function.get(
                    "parameters", {"type": "object", "properties": {}}
                ),
            }
        )
    return specs


def text_input(text: str) -> dict:
    """Build a Codex app-server text input item."""
    return {"type": "text", "text": text, "text_elements": []}


def _first_present(mapping: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def approval_response(method: str) -> tuple[dict | None, dict | None]:
    """Return ``(result, error)`` for app-server requests CORSIA handles."""
    if method == "item/commandExecution/requestApproval":
        return {"decision": "decline"}, None
    if method == "item/fileChange/requestApproval":
        return {"decision": "decline"}, None
    if method == "execCommandApproval":
        return {"decision": "denied"}, None
    if method == "applyPatchApproval":
        return {"decision": "denied"}, None
    return None, {
        "code": -32601,
        "message": f"Unsupported server request: {method}",
    }


def dynamic_tool_request(method: str) -> bool:
    """Return true when Codex is asking CORSIA to execute a dynamic tool.

    Codex 0.142.x sends the dynamic tool invocation as ``item/tool/call``
    (DynamicToolCallParams). The other names are kept as cross-version aliases.
    """
    return method in {
        "item/tool/call",
        "item/dynamicToolCall/request",
        "dynamicToolCall/request",
        "tool/execute",
        "tool/call",
    }


def provider_tool_violation_event(
    item: dict,
    *,
    phase: str,
    provider_thread_id: str | None = None,
    provider_turn_id: str | None = None,
) -> dict:
    """Record provider-native tool use as a contract violation, not a tool row."""
    raw_type = item.get("type")
    item_type = (
        raw_type
        if isinstance(raw_type, str) and raw_type in _NATIVE_ACTION_TYPES
        else "unknown-provider-action"
    )
    event = {
        "kind": "provider_tool_violation",
        "provider": "codex",
        "tool_type": item_type,
        "phase": phase,
    }
    if item.get("id") is not None:
        event["id"] = item.get("id")
    if provider_thread_id is not None or provider_turn_id is not None:
        event["provider_thread_id"] = provider_thread_id
        event["provider_turn_id"] = provider_turn_id
    return event


def item_started_event(
    item: dict,
    *,
    provider_thread_id: str | None = None,
    provider_turn_id: str | None = None,
) -> dict | None:
    """Convert an app-server started item into a runtime tool-use event."""
    item_type = item.get("type")
    if item_type == "contextCompaction":
        return {
            "kind": "context_compaction_started",
            "provider_thread_id": provider_thread_id,
            "provider_turn_id": provider_turn_id,
            "provider_item_id": item.get("id"),
        }
    if item_type == "dynamicToolCall":
        event = {
            "kind": "tool_use",
            "id": item.get("id"),
            "name": _first_present(item, "tool", "name", default=item_type),
            "input": item.get("arguments") or {},
        }
        if provider_thread_id is not None or provider_turn_id is not None:
            event["provider_thread_id"] = provider_thread_id
            event["provider_turn_id"] = provider_turn_id
        return event
    if item_type in _PASSIVE_ITEM_TYPES:
        return None
    return provider_tool_violation_event(
        item,
        phase="started",
        provider_thread_id=provider_thread_id,
        provider_turn_id=provider_turn_id,
    )


def _completed_reasoning_event(
    item: dict,
    *,
    summary_was_streamed: bool,
    opaque_reference_was_streamed: bool,
) -> dict | None:
    raw_summary = item.get("summary")
    summary = raw_summary if isinstance(raw_summary, list) else []
    text = "\n\n".join(
        part for part in summary if isinstance(part, str) and part
    )
    if summary_was_streamed or (not text and opaque_reference_was_streamed):
        return None
    return {
        "kind": "step_reasoning",
        "text": text,
        "provenance": reasoning_provenance(
            item.get("id"),
            representation_kind="codex_reasoning_summary" if text else "opaque",
        ),
    }


def _completed_tool_event(item: dict) -> dict:
    result = _first_present(item, "result", "contentItems", "error", default="")
    return {
        "kind": "tool_result",
        "tool_use_id": item.get("id"),
        "content": json.dumps(result) if not isinstance(result, str) else result,
        "is_error": bool(item.get("error")) or item.get("success") is False,
    }


def _with_provider_identity(
    event: dict,
    provider_thread_id: str | None,
    provider_turn_id: str | None,
) -> dict:
    if provider_thread_id is not None or provider_turn_id is not None:
        event["provider_thread_id"] = provider_thread_id
        event["provider_turn_id"] = provider_turn_id
    return event


def item_completed_event(
    item: dict,
    *,
    summary_was_streamed: bool = False,
    opaque_reference_was_streamed: bool = False,
    provider_thread_id: str | None = None,
    provider_turn_id: str | None = None,
) -> dict | None:
    """Convert an app-server completed item into a runtime result event."""
    item_type = item.get("type")
    if item_type in _COMPACTION_ITEM_TYPES:
        return provider_compaction_breach_event(
            source="item/completed",
            provider_thread_id=provider_thread_id,
            provider_turn_id=provider_turn_id,
            provider_item_id=(
                str(item["id"])
                if isinstance(item.get("id"), str) and item["id"]
                else None
            ),
        )
    if item_type == "agentMessage":
        event: dict[str, object] = {"kind": "assistant_text", "text": item.get("text", "")}
        if isinstance(item.get("id"), str) and item["id"]:
            # Codex's own item id, which its rollout also names the message by.
            event["provider_message_id"] = item["id"]
        return _with_provider_identity(event, provider_thread_id, provider_turn_id)
    if item_type == "reasoning":
        return _completed_reasoning_event(
            item,
            summary_was_streamed=summary_was_streamed,
            opaque_reference_was_streamed=opaque_reference_was_streamed,
        )
    if item_type == "dynamicToolCall":
        return _with_provider_identity(
            _completed_tool_event(item), provider_thread_id, provider_turn_id
        )
    if item_type in _PASSIVE_ITEM_TYPES:
        return None
    return provider_tool_violation_event(
        item,
        phase="completed",
        provider_thread_id=provider_thread_id,
        provider_turn_id=provider_turn_id,
    )


def result_event(turn: dict, usage: object) -> dict:
    """Build the runtime turn-result event from an app-server turn payload."""
    status = turn.get("status")
    result = "Codex turn interrupted." if status == "interrupted" else ""
    return {
        "kind": "result",
        "result": result,
        "is_error": False,
        "stop_reason": status if status else None,
        "usage": usage_payload(usage),
    }


def provider_compaction_breach_event(
    *,
    source: str | None = None,
    provider_thread_id: str | None = None,
    provider_turn_id: str | None = None,
    provider_item_id: str | None = None,
) -> dict:
    """Report that provider compaction progressed past the start tripwire."""
    return {
        "kind": "provider_compaction_breach",
        "source": source,
        "provider_thread_id": provider_thread_id,
        "provider_turn_id": provider_turn_id,
        "provider_item_id": provider_item_id,
    }


def failed_event(error: dict, usage: object = None) -> dict:
    """Build the runtime failure event from an app-server error payload."""
    event = {
        "kind": "turn_failed",
        "cause": codex_error_cause(error),
        "message": error.get("message"),
    }
    normalized_usage = usage_payload(usage)
    if normalized_usage is not None:
        event["usage"] = normalized_usage
    return event


def exact_token_count(value: Any) -> int | None:
    """Normalize nonnegative integral provider telemetry without truncation."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (
        not math.isfinite(value) or not value.is_integer()
    ):
        return None
    normalized = int(value)
    return normalized if normalized >= 0 else None


def reasoning_provenance(
    native_reference: Any,
    *,
    representation_kind: str = "codex_reasoning_summary",
) -> dict[str, Any]:
    """Return typed provider evidence; persistence performs canonicalization."""
    provenance: dict[str, Any] = {
        "source_harness": "codex",
        "source_provider": "codex",
        "representation_kind": representation_kind,
    }
    if isinstance(native_reference, str) and native_reference:
        provenance["native_reference"] = native_reference
    return provenance


def usage_payload(usage: object) -> dict | None:
    """Extract final-call occupancy and diagnostics for one Codex turn.

    ``last.inputTokens`` is the effective occupancy. Cached input is already a
    subset of that value and remains diagnostic; callers must not add it.
    """
    if not isinstance(usage, dict):
        return None
    last = usage.get("last")
    if not isinstance(last, dict):
        return None
    input_tokens = exact_token_count(last.get("inputTokens"))
    if input_tokens is None:
        return None
    cached_input_tokens = exact_token_count(last.get("cachedInputTokens"))
    if cached_input_tokens is None:
        cached_input_tokens = 0
    payload = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
    }
    # ThreadTokenUsage carries the observed window; absent means unknown.
    window = exact_token_count(usage.get("modelContextWindow"))
    if window is not None and window > 0:
        payload["model_context_window"] = window
    return payload


def codex_error_cause(error: dict) -> str:
    """Return the stable Codex error cause for runtime error markers."""
    from subagents.provider_context_limits import is_provider_context_limit

    if is_provider_context_limit(error):
        return "context_window_exceeded"
    info = error.get("codexErrorInfo")
    if isinstance(info, str):
        return info
    if isinstance(info, dict):
        return next(iter(info), "other")
    return "process_exited"
