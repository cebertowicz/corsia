"""litellm-shaped response objects the E2E mock returns."""

from __future__ import annotations

from types import SimpleNamespace


def _make_chunk(
    *,
    content: str | None = None,
    reasoning_content: str | None = None,
    finish_reason: str | None = None,
    tool_calls: list[dict] | None = None,
) -> SimpleNamespace:
    """Build a litellm-style streaming chunk."""
    tc_payload = None
    if tool_calls:
        tc_payload = []
        for i, tc in enumerate(tool_calls):
            tc_payload.append(SimpleNamespace(
                index=i,
                id=tc.get("id", f"tc_{i}"),
                function=SimpleNamespace(
                    name=tc.get("name", ""),
                    arguments=tc.get("arguments_json", tc.get("arguments", "{}")),
                ),
            ))
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning_content,
        tool_calls=tc_payload,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=None)


def _make_non_streaming(reply: dict) -> SimpleNamespace:
    """Build a litellm-style non-streaming response."""
    message = SimpleNamespace(
        content=reply.get("content"),
        reasoning_content=reply.get("reasoning_content"),
        tool_calls=None,
    )
    choice = SimpleNamespace(
        message=message,
        finish_reason=reply.get("finish_reason", "stop"),
    )
    usage = SimpleNamespace(
        prompt_tokens=reply.get("prompt_tokens", 100),
        completion_tokens=reply.get("completion_tokens", 50),
        total_tokens=reply.get("total_tokens", 150),
    )
    return SimpleNamespace(choices=[choice], usage=usage)
