"""Pure translation + parsing helpers for the OpenAI Responses provider module.

No SDK calls, no asyncio, no I/O — just shape conversion between CORSIA's
OpenAI-chat-completion-style messages/tools and the Responses API shape.
Imported by ``openai.py`` (transport)
and ``openai_streaming.py``.
"""

from __future__ import annotations

from typing import Any

import json_repair
from loguru import logger


def _messages_to_responses_input(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Translate chat messages into (instructions, input_items) for Responses API.

    Returns:
        (instructions, input_items) — instructions is the first system message,
        input_items is everything else translated to Responses API format.
    """
    instructions: str | None = None
    items: list[dict[str, Any]] = []
    seen_system = False

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content")

        if role == "system":
            if not seen_system:
                instructions = content if isinstance(content, str) else str(content)
                seen_system = True
            else:
                items.append({"role": "developer", "content": _to_text(content)})

        elif role == "user":
            items.append({"role": "user", "content": _to_text(content)})

        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            text = _to_text(content)
            if text:
                items.append({"role": "assistant", "content": text})
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    items.append({
                        "type": "function_call",
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", "{}"),
                        "call_id": tc.get("id", ""),
                    })

        elif role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id", ""),
                "output": _to_text(content),
            })

    return instructions, items


def _to_text(content: Any) -> str:
    """Extract plain text from content (string or content-block list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content) if content else ""


def _tools_to_responses_format(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Flatten chat-completions tool format to Responses API format.

    Chat completions: {type: "function", function: {name, description, parameters}}
    Responses API:    {type: "function", name, description, parameters}
    """
    if not tools:
        return None
    result = []
    for tool in tools:
        fn = tool.get("function", {})
        result.append({
            "type": "function",
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
        })
    return result


def _parse_responses_usage(usage: Any) -> dict[str, int | None]:
    """Use one disjoint accounting shape for streaming and ordinary Responses."""
    from infra import provider_usage

    details = getattr(usage, "input_tokens_details", None)
    measured = provider_usage.from_openai({
        "prompt_tokens": getattr(usage, "input_tokens", None),
        "completion_tokens": getattr(usage, "output_tokens", None),
        "prompt_tokens_details": {"cached_tokens": getattr(details, "cached_tokens", None)},
    })
    return measured.ledger_row() if measured is not None else provider_usage.unmeasured_ledger_row()


def _parse_responses_response(response: Any, model: str) -> "LLMResponse":
    """Translate ResponsesAPIResponse into LLMResponse."""
    from llm.router import LLMResponse, ToolCallRequest

    text = response.output_text or None
    if text == "":
        text = None

    tool_calls = []
    reasoning_parts: list[str] = []
    reasoning_references: dict[str, None] = {}
    for item in response.output:
        item_type = getattr(item, "type", None)
        if item_type == "function_call":
            args = item.arguments
            if isinstance(args, str):
                args = json_repair.loads(args)
            tool_calls.append(ToolCallRequest(
                id=item.call_id,
                name=item.name,
                arguments=args,
            ))
        elif item_type == "reasoning":
            summaries = getattr(item, "summary", [])
            if summaries:
                reasoning_parts.extend(
                    s.text for s in summaries
                    if isinstance(getattr(s, "text", None), str) and s.text
                )
                reference = getattr(item, "id", "") or ""
                if reference:
                    reasoning_references.setdefault(reference, None)

    status = getattr(response, "status", "completed")
    finish_reason = "length" if status == "incomplete" else "stop"

    usage = {}
    if response.usage:
        u = response.usage
        usage = _parse_responses_usage(u)
        logger.debug(
            "responses_api_usage: input_tokens={} output_tokens={} total={}",
            u.input_tokens, u.output_tokens, u.total_tokens,
        )

    cost_usd: float | None = None
    try:
        from llm.pricing import calculate_cost
        cost_usd = calculate_cost(model, usage)
    except Exception as e:
        # silent-swallow: pricing is metadata; turn must succeed even
        # if cost calc fails. Log so cost-attribution gaps surface.
        logger.warning(f"openai cost calc failed for {model}: {e}")
    usage["cost_usd"] = cost_usd
    usage["model"] = model
    reasoning_content = "\n".join(reasoning_parts) or None
    reasoning_provenance: dict[str, Any] | None = None
    if reasoning_content is not None:
        reasoning_provenance = {"representation_kind": "opaque"}
        native_references = list(reasoning_references)
        if native_references:
            reasoning_provenance["native_references"] = native_references
            if len(native_references) == 1:
                reasoning_provenance["native_reference"] = native_references[0]

    return LLMResponse(
        content=text,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=usage,
        reasoning_content=reasoning_content,
        reasoning_provenance=reasoning_provenance,
        reported_model=(
            str(response.model).strip()
            if isinstance(getattr(response, "model", None), str)
            and str(response.model).strip()
            else None
        ),
    )


def _strip_openai_prefix(model: str) -> str:
    """Strip the `openai/` provider prefix.

    The official OpenAI SDK doesn't accept the litellm-style ``openai/``
    prefix on the ``model`` field — it expects just ``gpt-5``, ``o1``, etc.
    CORSIA stores the prefixed form throughout (catalog rows, dispatch),
    so we strip on the way out the door."""
    return model.removeprefix("openai/") if model else ""


def _reasoning_active(effort: str | None) -> bool:
    """True iff a real reasoning request will be made for this turn.

    "auto" (let the model decide) and "none" (explicitly off) read as
    inactive — we don't engage reasoning for either, and the call behaves
    as a non-reasoning completion.
    """
    if not effort:
        return False
    return effort.strip().lower() not in ("auto", "none", "")


_ARESPONSES_KWARGS = frozenset({
    "model", "input", "instructions", "tools", "tool_choice",
    "max_output_tokens", "reasoning", "stream",
})
