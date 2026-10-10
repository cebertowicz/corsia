"""Normalize Claude Code assistant, user, and streaming message frames."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from core.reasoning_provenance import normalize_reasoning_provenance


@dataclass
class _AssistantParts:
    text: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    tool_uses: list[dict] = field(default_factory=list)
    signatures: list[str] = field(default_factory=list)
    reasoning_blocks: list[dict[str, Any]] = field(default_factory=list)
    saw_thinking: bool = False


def _thinking_provenance(parts: _AssistantParts, model: Any) -> dict[str, Any]:
    return normalize_reasoning_provenance(
        source_harness="claude-code", source_provider="anthropic",
        source_effective_model=model if isinstance(model, str) and model else None,
        representation_kind="anthropic_thinking",
        native_signatures=parts.signatures, native_blocks=parts.reasoning_blocks,
    )


def _append_thinking(parts: _AssistantParts, block: dict) -> None:
    thinking = block.get("thinking")
    signature = block.get("signature")
    if isinstance(thinking, str):
        parts.saw_thinking = True
        parts.reasoning.append(thinking)
    if isinstance(signature, str) and signature:
        parts.saw_thinking = True
        parts.signatures.append(signature)
    if not isinstance(thinking, str) and not (
        isinstance(signature, str) and signature
    ):
        return
    native = {
        "type": "thinking",
        "thinking": thinking if isinstance(thinking, str) else "",
    }
    if isinstance(signature, str) and signature:
        native["signature"] = signature
    parts.reasoning_blocks.append(native)


def _append_redacted_thinking(parts: _AssistantParts, block: dict) -> None:
    data = block.get("data")
    if isinstance(data, str) and data:
        parts.saw_thinking = True
        parts.reasoning_blocks.append({"type": "redacted_thinking", "data": data})


def _append_tool_use(parts: _AssistantParts, block: dict) -> None:
    tool_input = block.get("input")
    tool_use = {
        "id": block.get("id"), "name": block.get("name"),
        "input": tool_input if isinstance(tool_input, dict) else {},
    }
    if not isinstance(tool_input, dict):
        tool_use["malformed"] = True
    parts.tool_uses.append(tool_use)


def _append_assistant_block(parts: _AssistantParts, block: object) -> None:
    if not isinstance(block, dict):
        return
    block_type = block.get("type")
    if block_type == "text" and isinstance(block.get("text"), str):
        parts.text.append(block["text"])
    elif block_type == "thinking":
        _append_thinking(parts, block)
    elif block_type == "redacted_thinking":
        _append_redacted_thinking(parts, block)
    elif block_type == "tool_use":
        _append_tool_use(parts, block)


def _assistant_parts(content: list) -> _AssistantParts:
    parts = _AssistantParts()
    for block in content:
        _append_assistant_block(parts, block)
    return parts


def from_assistant(message: dict) -> list[dict]:
    """Normalize one assistant message without splitting its Assistant Step."""
    content = message.get("content") or [] if isinstance(message, dict) else []
    if not isinstance(content, list):
        return []
    parts = _assistant_parts(content)
    provenance = (
        _thinking_provenance(parts, message.get("model"))
        if parts.saw_thinking else None
    )
    event: dict[str, Any]
    if not parts.text and not any(parts.reasoning) and not parts.tool_uses:
        if provenance is None:
            return []
        event = {"kind": "reasoning_provenance", "reasoning_provenance": provenance}
    else:
        event = {
            "kind": "assistant_step", "text": "\n".join(parts.text),
            "reasoning": "\n".join(parts.reasoning), "tool_uses": parts.tool_uses,
        }
        if provenance is not None:
            event["reasoning_provenance"] = provenance
    message_id = message.get("id")
    if isinstance(message_id, str) and message_id:
        # Claude's own message.id: the only name its live transcript gives the
        # row a checkpoint must later prove.
        event["provider_message_id"] = message_id
    return [event]


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            if isinstance(block.get("text"), str):
                parts.append(block["text"])
        elif isinstance(block, dict) and block.get("type") == "image":
            source = block.get("source")
            media = source.get("media_type") if isinstance(source, dict) else None
            parts.append(f"[image: {media or 'image'}]")
    return "\n".join(parts)


def from_user(message: dict) -> list[dict]:
    content = message.get("content") or [] if isinstance(message, dict) else []
    if not isinstance(content, list):
        return []
    return [{
        "kind": "tool_result", "tool_use_id": block.get("tool_use_id"),
        "content": _tool_result_text(block.get("content")),
        "is_error": bool(block.get("is_error")),
    } for block in content
      if isinstance(block, dict) and block.get("type") == "tool_result"]


def thinking_provenance(signature: str) -> dict[str, Any]:
    return _thinking_provenance(_AssistantParts(signatures=[signature]), None)


def _tool_input_block(event: dict) -> list[dict]:
    """A tool-use block opening, with its call id and name, or any block closing, by stream index."""
    if event.get("type") == "content_block_stop":
        return [{"kind": "tool_input_stop", "index": event.get("index")}]
    block = event.get("content_block")
    if not isinstance(block, dict) or block.get("type") != "tool_use":
        return []
    return [{"kind": "tool_input_start", "index": event.get("index"),
             "call_id": block.get("id"), "name": block.get("name")}]


def from_stream_event(event: dict) -> list[dict]:
    if not isinstance(event, dict):
        return []
    if event.get("type") == "message_delta":
        usage = event.get("usage")
        return [{
            "kind": "final_call_usage",
            "usage": dict(usage) if isinstance(usage, dict) else None,
        }]
    if event.get("type") in {"content_block_start", "content_block_stop"}:
        return _tool_input_block(event)
    if event.get("type") != "content_block_delta":
        return []
    delta = event.get("delta") or {}
    if not isinstance(delta, dict):
        return []
    if delta.get("type") == "input_json_delta":
        fragment = delta.get("partial_json")
        return ([{"kind": "tool_input_delta", "index": event.get("index"), "text": fragment}]
                if isinstance(fragment, str) and fragment else [])
    event_shape = {
        "text_delta": ("text_delta", "text"),
        "thinking_delta": ("thinking_delta", "thinking"),
    }.get(cast(str, delta.get("type")))
    if event_shape is not None:
        kind, key = event_shape
        value = delta.get(key)
        return [{"kind": kind, "text": value}] if isinstance(value, str) else []
    signature = delta.get("signature")
    if delta.get("type") == "signature_delta" and isinstance(signature, str) and signature:
        return [{
            "kind": "reasoning_provenance",
            "reasoning_provenance": thinking_provenance(signature),
        }]
    return []
