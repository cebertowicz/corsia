"""Translation and parsing helpers for the Anthropic SDK provider module."""

from __future__ import annotations

from typing import Any

import json_repair
from loguru import logger


def _apply_cache_control(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    """Inject Anthropic cache_control breakpoints for prompt caching."""
    _CACHE = {"cache_control": {"type": "ephemeral"}}

    def _with_cache_breakpoint(msg: dict[str, Any]) -> dict[str, Any]:
        content = msg.get("content")
        if isinstance(content, str) and content:
            return {
                **msg,
                "content": [{"type": "text", "text": content, **_CACHE}],
            }
        if isinstance(content, list) and content:
            new_content = list(content)
            for block_index in range(len(new_content) - 1, -1, -1):
                block = new_content[block_index]
                if isinstance(block, str) and block:
                    new_content[block_index] = {
                        "type": "text",
                        "text": block,
                        **_CACHE,
                    }
                    return {**msg, "content": new_content}
                if (
                    isinstance(block, dict)
                    and block.get("type") in {"text", "tool_use", "tool_result"}
                ):
                    new_content[block_index] = {**block, **_CACHE}
                    return {**msg, "content": new_content}
        if msg.get("role") == "assistant":
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                new_tool_calls = list(tool_calls)
                last_tool = new_tool_calls[-1]
                if isinstance(last_tool, dict):
                    new_tool_calls[-1] = {**last_tool, **_CACHE}
                    return {**msg, "tool_calls": new_tool_calls}
        return msg

    new_messages = []

    user_indices = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    history_breakpoint = user_indices[-2] if len(user_indices) >= 2 else -1

    n = len(messages)
    turn_breakpoint = -1
    if n >= 4:
        for candidate in range(n - 2, 0, -1):
            msg_c = messages[candidate]
            role = msg_c.get("role", "")
            content = msg_c.get("content")
            has_text = (isinstance(content, str) and content) or (isinstance(content, list) and content)
            if role in ("user", "assistant") and has_text and candidate > history_breakpoint:
                turn_breakpoint = candidate
                break

    for i, msg in enumerate(messages):
        if msg.get("role") == "system":
            new_messages.append(_with_cache_breakpoint(msg))
        elif i == history_breakpoint or i == turn_breakpoint:
            new_messages.append(_with_cache_breakpoint(msg))
        else:
            new_messages.append(msg)

    new_tools = tools
    if tools:
        new_tools = list(tools)
        new_tools[-1] = {**new_tools[-1], **_CACHE}

    return new_messages, new_tools


def _translate_content_blocks(content: Any) -> list[dict[str, Any]] | str | None:
    """Translate an OpenAI-shaped content value into Anthropic shape."""
    if content is None or isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    out: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            if isinstance(block, str) and block:
                out.append({"type": "text", "text": block})
            continue

        btype = block.get("type")

        if btype == "image_url":
            url = (block.get("image_url") or {}).get("url", "")
            if url.startswith("data:"):
                try:
                    header, data = url.split(",", 1)
                    media_type = header.split(":", 1)[1].split(";", 1)[0]
                except (ValueError, IndexError):
                    logger.warning("dropping malformed data URL in image content block")
                    continue
                out.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": data},
                })
            else:
                out.append({"type": "image", "source": {"type": "url", "url": url}})
        else:
            out.append(block)

    return out or None


def _translate_assistant_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Translate an assistant message into Anthropic content blocks."""
    blocks: list[dict[str, Any]] = []
    translated = _translate_content_blocks(msg.get("content"))
    if isinstance(translated, str) and translated:
        blocks.append({"type": "text", "text": translated})
    elif isinstance(translated, list):
        blocks.extend(translated)

    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            parsed = json_repair.loads(args) if args.strip() else {}
        else:
            parsed = args or {}
        if not isinstance(parsed, dict):
            parsed = {}
        tool_block = {
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": fn.get("name", ""),
            "input": parsed,
        }
        if "cache_control" in tc:
            tool_block["cache_control"] = tc["cache_control"]
        blocks.append(tool_block)

    if not blocks:
        return None
    return {"role": "assistant", "content": blocks}


def _translate_messages_for_anthropic_sdk(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]] | str | None, list[dict[str, Any]]]:
    """Translate CORSIA/OpenAI-shaped messages into Anthropic SDK parameters."""
    system_blocks: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role", "")
        raw_content = msg.get("content")

        if role == "system":
            content = _translate_content_blocks(raw_content)
            if isinstance(content, str):
                system_blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                system_blocks.extend(content)
            continue

        if role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            text = raw_content if isinstance(raw_content, str) else _stringify_content(raw_content)
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": text or "",
                }],
            })
            continue

        if role == "assistant":
            assistant_msg = _translate_assistant_message(msg)
            if assistant_msg is not None:
                out.append(assistant_msg)
            continue

        translated = _translate_content_blocks(raw_content)
        if translated is None or (isinstance(translated, str) and not translated):
            translated = "(empty)"
        out.append({"role": "user", "content": translated})

    if not system_blocks:
        system_param: list[dict[str, Any]] | str | None = None
    elif (
        len(system_blocks) == 1
        and system_blocks[0].get("type") == "text"
        and "cache_control" not in system_blocks[0]
    ):
        system_param = system_blocks[0]["text"]
    else:
        system_param = system_blocks

    return system_param, out


def _stringify_content(content: Any) -> str:
    """Coerce mixed content into plain text for tool results."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                else:
                    parts.append(str(block))
        return "\n".join(p for p in parts if p)
    return str(content)


def _translate_tools_for_anthropic_sdk(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Translate OpenAI-shaped tools into Anthropic shape."""
    if not tools:
        return None

    out: list[dict[str, Any]] = []
    for tool in tools:
        if "input_schema" in tool or "function" not in tool:
            out.append(tool)
            continue
        fn = tool.get("function") or {}
        translated: dict[str, Any] = {
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        }
        if "cache_control" in tool:
            translated["cache_control"] = tool["cache_control"]
        out.append(translated)
    return out


def _map_stop_reason(stop_reason: str | None) -> str:
    """Anthropic ``stop_reason`` to OpenAI-flavoured ``finish_reason``."""
    if not stop_reason:
        return "stop"
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "model_context_window_exceeded": "length",
        "tool_use": "tool_calls",
        "refusal": "stop",
        "pause_turn": "stop",
    }.get(stop_reason, "stop")


def _parse_usage(u: Any) -> dict[str, int | None]:
    """Translate an SDK ``Usage`` object into CORSIA's usage dict shape."""
    from infra import provider_usage

    measured = provider_usage.from_anthropic({
        field: getattr(u, field, None) for field in (
            "input_tokens", "output_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    })
    return measured.ledger_row() if measured is not None else provider_usage.unmeasured_ledger_row()


def _annotate_usage(usage: dict[str, Any], model: str) -> dict[str, Any]:
    """Attach cost + model fields the rest of CORSIA relies on."""
    cost_usd: float | None = None
    try:
        from llm.pricing import calculate_cost
        cost_usd = calculate_cost(model, usage)
    except Exception as e:
        from loguru import logger
        logger.warning(f"anthropic cost calc failed for {model}: {e}")
    usage["cost_usd"] = cost_usd
    usage["model"] = model
    return usage
