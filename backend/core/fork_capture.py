"""Shared capture outcomes for disposable provider forks."""

from __future__ import annotations

from loguru import logger


def warm_capture_refused(thread_id: str, harness: str, reason: str) -> None:
    """Log one refusal to capture a warm source, then return no source."""
    logger.info(
        "fork warm capture refused for {} ({}): {}",
        thread_id, harness, reason,
    )
    return None


def warm_boundary(projection, boundary):
    """Return the harness-neutral descriptor of the durable through row."""
    if boundary is None:
        return None
    matches = [
        message
        for message in projection.messages
        if message.seq == boundary.seq and message.message_id == boundary.message_id
    ]
    if len(matches) != 1:
        return None
    message = matches[0]
    if message.role not in {"user", "assistant", "tool"}:
        return None
    if message.role == "tool" and not message.tool_call_id:
        return None
    # Only the final row of a provider record group is a provable prefix cut.
    if message.provider_message_id and any(
        later.seq > message.seq
        and later.provider_message_id == message.provider_message_id
        for later in projection.messages
    ):
        return None
    assistant_step_id = (
        message.assistant_step_id
        if message.role == "tool"
        else (message.message_id if message.role == "assistant" else None)
    )
    assistant = next(
        (
            candidate
            for candidate in projection.messages
            if assistant_step_id is not None
            and candidate.role == "assistant"
            and candidate.message_id == assistant_step_id
        ),
        None,
    )
    tool_call_ids = tuple(
        str(call["id"])
        for call in (assistant.tool_calls if assistant is not None else ())
        if isinstance(call, dict) and isinstance(call.get("id"), str) and call["id"]
    )
    from history.types import DurableBoundaryMessage

    return DurableBoundaryMessage(
        role=message.role,
        content=message.content,
        tool_call_id=message.tool_call_id if message.role == "tool" else None,
        message_id=message.message_id,
        assistant_step_id=assistant_step_id,
        tool_call_ids=tool_call_ids,
        provider_message_id=message.provider_message_id,
    )
