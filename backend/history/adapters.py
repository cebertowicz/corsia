"""Provider-facing renderers for one portable transcript projection."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from core.message_history import frame_agent_envelope
from core.reasoning_provenance import anthropic_thinking_blocks_for_replay
from core.thread_lifecycle import TOOL_TRIM_MARKER
from history.tool_argument_projection import (
    project_checkpoint_tool_call,
    render_checkpoint_tool_call,
)
from history.types import (
    PortableHistoryError,
    PortableHistoryProjection,
    PortableMessage,
)
from tools.catalog import normalize_tool_name


def render_labeled_artifact_references(
    content: str,
    artifact_references: tuple[dict[str, Any], ...],
) -> str:
    """Append portable artifact identities without changing transcript order."""
    if not artifact_references:
        return content
    rendered = "\n".join(
        "- " + json.dumps(reference, sort_keys=True, separators=(",", ":"))
        for reference in artifact_references
    )
    section = "[CORSIA artifact references]\n" + rendered
    return f"{content}\n\n{section}" if content else section


MID_TURN_RESUME_ANCHOR = (
    "[CORSIA] The checkpoint above covers the earlier part of this same turn. "
    "The verbatim transcript resumes mid-turn; continue the work in progress "
    "rather than restarting it."
)


def mid_turn_resume_text(presence_line: str | None = None) -> str:
    """The resume anchor, followed directly by the install-frozen presence line.

    One text for every harness and every estimate, so the line never renders
    without the anchor and no caller can count one and forget the other.
    """
    if not presence_line:
        return MID_TURN_RESUME_ANCHOR
    return f"{MID_TURN_RESUME_ANCHOR}\n{presence_line}"


def _mid_turn_resume_anchor(
    projection: PortableHistoryProjection,
) -> list[dict[str, Any]]:
    """Restore the user-role anchor when a selection starts mid-Turn.

    Providers require the replayed message list to open on a ``user`` role,
    and the rest of CORSIA depends on it (see
    ``thread_history._trim_to_window``). A Turn-granular tail satisfies that
    for free because every Turn opens with user input; a tail taken from an
    interior cut point opens on an Assistant Step instead.

    This is a rendering-only bridge, exactly like the provider-safe recasting
    in :func:`checkpoint_source_block`. No message row and no Turn row is
    invented: the durable transcript is untouched and the fragment keeps its
    original ``turn_id``.
    """
    turns = projection.turns
    if not turns or turns[0].includes_turn_start or not turns[0].messages:
        return []
    if turns[0].messages[0].role == "user":
        return []
    return [{
        "role": "user",
        "content": mid_turn_resume_text(projection.resume_presence_line),
    }]


def _native_user_message(source: PortableMessage) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "user",
        "content": render_labeled_artifact_references(
            frame_agent_envelope(source.content, source.inbound_source),
            source.artifact_references,
        ),
    }
    if source.inbound_source:
        message["inbound_source"] = dict(source.inbound_source)
    return message


def _native_assistant_message(
    source: PortableMessage,
    *,
    destination_provider: str | None,
    destination_effective_model: str | None,
    tool_argument_budget_tokens: int | None,
) -> dict[str, Any]:
    assistant_content = render_labeled_artifact_references(
        source.content,
        source.artifact_references,
    )
    native_blocks = anthropic_thinking_blocks_for_replay(
        source.reasoning_provenance,
        destination_provider=destination_provider,
        destination_effective_model=destination_effective_model,
    )
    content: str | list[dict[str, Any]] = assistant_content
    if native_blocks:
        content = native_blocks
        if assistant_content:
            content.append({"type": "text", "text": assistant_content})
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if source.tool_calls:
        projected_calls = []
        for call in source.tool_calls:
            projected = project_checkpoint_tool_call(
                source.message_id,
                call,
                tool_argument_budget_tokens,
            )
            function = projected.get("function")
            if isinstance(function, dict):
                projected["function"] = {
                    **function,
                    "name": normalize_tool_name(function.get("name")),
                }
            projected_calls.append(projected)
        message["tool_calls"] = projected_calls
    if source.reasoning_content is not None:
        message["reasoning_content"] = source.reasoning_content
    return message


def _native_tool_message(
    projection: PortableHistoryProjection,
    source: PortableMessage,
    *,
    tool_result_budget_tokens: int | None,
) -> dict[str, Any]:
    from core.tool_result_projection import render_tool_result_for_model

    return {
        "role": "tool",
        "tool_call_id": source.tool_call_id or "",
        "name": normalize_tool_name(source.tool_name),
        "content": render_labeled_artifact_references(
            render_tool_result_for_model(
                projection.thread_id,
                source,
                budget_tokens=tool_result_budget_tokens,
            ),
            source.artifact_references,
        ),
    }


def to_native_history(
    projection: PortableHistoryProjection,
    *,
    destination_provider: str | None = None,
    destination_effective_model: str | None = None,
    tool_result_budget_tokens: int | None = None,
    tool_argument_budget_tokens: int | None = None,
) -> list[dict[str, Any]]:
    """Translate a selected projection to the canonical Native message shape.

    A projection holed by retention translates like any other: the trimmed row
    carries its own ``TOOL_TRIM_MARKER`` text, and the selection that chose it
    declares the loss in band. Refusing here instead used to leave a trimmed
    thread with nothing to send.
    """
    messages: list[dict[str, Any]] = _mid_turn_resume_anchor(projection)
    for source in projection.messages:
        if source.role == "user":
            messages.append(_native_user_message(source))
        elif source.role == "assistant":
            messages.append(
                _native_assistant_message(
                    source,
                    destination_provider=destination_provider,
                    destination_effective_model=destination_effective_model,
                    tool_argument_budget_tokens=tool_argument_budget_tokens,
                )
            )
        elif source.role == "tool":
            messages.append(
                _native_tool_message(
                    projection,
                    source,
                    tool_result_budget_tokens=tool_result_budget_tokens,
                )
            )
        else:  # pragma: no cover - PortableMessage construction owns roles
            raise PortableHistoryError(f"unsupported portable role {source.role!r}")
    return messages


def _checkpoint_user_block(message: PortableMessage, parts: list[str]) -> dict[str, str]:
    content = render_labeled_artifact_references(
        frame_agent_envelope(message.content, message.inbound_source),
        message.artifact_references,
    )
    if content:
        parts.append(content)
    return {"role": "user", "content": "\n".join(parts)}


def _checkpoint_assistant_block(
    message: PortableMessage,
    parts: list[str],
    tool_argument_budget_tokens: int | None,
) -> dict[str, str]:
    if message.reasoning_text:
        parts.append(f"[reasoning]\n{message.reasoning_text}")
    content = render_labeled_artifact_references(
        message.content, message.artifact_references
    )
    if content:
        parts.append(content)
    for call in message.tool_calls:
        rendered = render_checkpoint_tool_call(
            message.message_id, call, tool_argument_budget_tokens
        )
        parts.append(f"[tool call {rendered}]")
    return {"role": "assistant", "content": "\n".join(parts)}


def _checkpoint_tool_block(
    message: PortableMessage,
    parts: list[str],
    thread_id: str | None,
    tool_result_budget_tokens: int | None,
    stub_body: bool = False,
) -> dict[str, str]:
    label = f"[tool result {message.tool_name or ''}".rstrip()
    if message.tool_call_id:
        label += f" call={message.tool_call_id}"
    if message.is_error:
        label += " error=true"
    if message.synthetic_tool_result:
        label += f" synthetic={message.synthetic_reason or 'unknown'}"
    parts.append(label + "]")
    if stub_body:
        from core.tool_result_projection import _verified_message_identity

        _verified_message_identity(message)
        parts.append("[result body omitted from this cold checkpoint input]")
        return {"role": "user", "content": "\n".join(parts)}
    from core.tool_result_projection import render_tool_result_for_model

    projected = render_tool_result_for_model(
        thread_id or "", message, budget_tokens=tool_result_budget_tokens
    )
    content = render_labeled_artifact_references(projected, message.artifact_references)
    if content.startswith(TOOL_TRIM_MARKER):
        content = (
            "[legacy destructively retained tool result — full bytes are unavailable]\n"
            + content
        )
    if content:
        parts.append(content)
    return {"role": "user", "content": "\n".join(parts)}


def checkpoint_source_block(
    message: PortableMessage,
    *,
    thread_id: str | None = None,
    tool_result_budget_tokens: int | None = None,
    tool_argument_budget_tokens: int | None = None,
    stub_tool_result_body: bool = False,
) -> dict[str, str]:
    """Quote one portable message, optionally stubbing its cold-only tool body."""
    parts = [f"[msg {message.message_id}]"]
    if message.role == "user":
        return _checkpoint_user_block(message, parts)
    if message.role == "assistant":
        return _checkpoint_assistant_block(message, parts, tool_argument_budget_tokens)
    if message.role == "tool":
        return _checkpoint_tool_block(
            message, parts, thread_id, tool_result_budget_tokens,
            stub_body=stub_tool_result_body,
        )
    raise PortableHistoryError(f"unsupported portable role {message.role!r}")


def checkpoint_retention_note(count: int) -> dict[str, str]:
    """Say plainly that part of the covered range is no longer available."""
    return {
        "role": "user",
        "content": (
            f"[note] {count} message(s) in this range were destructively "
            "retained; their content is unavailable."
        ),
    }


def merge_checkpoint_source_blocks(
    blocks: Sequence[dict[str, str]],
) -> list[dict[str, str]]:
    """Coalesce consecutive same-role blocks into a provider-valid alternation."""
    merged: list[dict[str, str]] = []
    for block in blocks:
        if merged and merged[-1]["role"] == block["role"]:
            merged[-1] = {
                "role": block["role"],
                "content": merged[-1]["content"] + "\n\n" + block["content"],
            }
            continue
        merged.append(dict(block))
    return merged


def to_checkpoint_source_messages(
    projection: PortableHistoryProjection,
    *,
    tool_result_budget_tokens: int | None = None,
    tool_argument_budget_tokens: int | None = None,
) -> list[dict[str, str]]:
    """Render a projection as checkpoint source blocks, unmerged and in order.

    Summarizing a thread that has had destructive retention is correct and
    expected, and the gap is named instead of hidden.

    Tool-result bodies use the same exact-or-bounded-reference projection as
    provider replay; no assistant-restatement heuristic drops older results.
    """
    blocks = [
        checkpoint_source_block(
            message,
            thread_id=projection.thread_id,
            tool_result_budget_tokens=tool_result_budget_tokens,
            tool_argument_budget_tokens=tool_argument_budget_tokens,
        )
        for message in projection.messages
    ]
    if projection.retention_stub_seqs:
        blocks.append(checkpoint_retention_note(len(projection.retention_stub_seqs)))
    return blocks
