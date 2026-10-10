"""Project canonical CORSIA history into Claude Code message wire shapes."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.message_history import frame_agent_envelope
from core.reasoning_provenance import anthropic_thinking_blocks_for_replay
from history.adapters import render_labeled_artifact_references
from history.tool_argument_projection import project_checkpoint_tool_call
from history.types import PortableHistoryError, PortableHistoryProjection, PortableMessage
from tools.catalog import normalize_tool_name


_CORSIA_MCP_PREFIX = "mcp__corsia__"


@dataclass(frozen=True)
class _CCProjectedMessage:
    source: PortableMessage
    row_type: str
    row_uuid: str
    message: dict[str, Any]


def _tool_input(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw in (None, ""):
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise PortableHistoryError(
                "Claude Code reconstruction requires object tool arguments"
            ) from exc
        if isinstance(parsed, dict):
            return parsed
    raise PortableHistoryError("Claude Code reconstruction requires object tool arguments")


def _assistant_blocks(
    source: PortableMessage,
    *,
    destination_provider: str | None,
    destination_effective_model: str | None,
    allowed_corsia_tool_names: frozenset[str],
    tool_argument_budget_tokens: int | None,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    blocks.extend(anthropic_thinking_blocks_for_replay(
        source.reasoning_provenance,
        destination_provider=destination_provider,
        destination_effective_model=destination_effective_model,
    ))
    content = render_labeled_artifact_references(
        source.content, source.artifact_references
    )
    if content.strip():
        blocks.append({"type": "text", "text": content})
    for call in source.tool_calls:
        if not isinstance(call, dict):
            raise PortableHistoryError("Claude Code tool call is not an object")
        projected = project_checkpoint_tool_call(
            source.message_id,
            call,
            tool_argument_budget_tokens,
        )
        function = projected.get("function") or {}
        source_name = function.get("name") or ""
        canonical_name = normalize_tool_name(source_name)
        destination_name = (
            f"{_CORSIA_MCP_PREFIX}{canonical_name}"
            if canonical_name in allowed_corsia_tool_names else source_name
        )
        blocks.append({
            "type": "tool_use", "id": call.get("id") or "",
            "name": destination_name, "input": _tool_input(function.get("arguments")),
        })
    if not blocks:
        blocks.append({"type": "text", "text": "(empty)"})
    return blocks


def _user_content(source: PortableMessage) -> str:
    framed = frame_agent_envelope(source.content, source.inbound_source)
    content = render_labeled_artifact_references(framed, source.artifact_references)
    return content if content != "" else "(empty)"


def _tool_result_content(
    source: PortableMessage, *, thread_id: str, budget_tokens: int | None
) -> str:
    from core.tool_result_projection import render_tool_result_for_model

    content = render_tool_result_for_model(
        thread_id, source, budget_tokens=budget_tokens
    )
    if source.synthetic_tool_result:
        marker = f"[CORSIA synthetic tool result: {source.synthetic_reason or 'unknown'}]"
        content = f"{marker}\n{content}" if content else marker
    return render_labeled_artifact_references(
        content, source.artifact_references
    ) or "(empty)"


_TRANSCRIPT_SEED = "corsia:claude-code:reconstructed-transcript:"


def _stable_uuid(*parts: str) -> str:
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        _TRANSCRIPT_SEED + ":".join(parts),
    ))


def message_timestamp(source: PortableMessage) -> str:
    raw = source.created_at.strip()
    if raw:
        return raw
    return datetime.fromtimestamp(source.seq, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _assistant_message_id(row_uuid: str) -> str:
    return "msg_corsia_" + row_uuid.replace("-", "")[:24]


def _project_message(
    source: PortableMessage,
    projection: PortableHistoryProjection,
    *,
    cc_session_id: str,
    destination_provider: str | None,
    destination_effective_model: str | None,
    allowed_corsia_tool_names: frozenset[str],
    tool_result_budget_tokens: int | None,
    tool_argument_budget_tokens: int | None,
) -> _CCProjectedMessage:
    row_uuid = _stable_uuid(cc_session_id, source.message_id, source.role)
    if source.role == "user":
        return _CCProjectedMessage(
            source, "user", row_uuid,
            {"role": "user", "content": _user_content(source)},
        )
    if source.role == "assistant":
        return _CCProjectedMessage(source, "assistant", row_uuid, {
            "id": _assistant_message_id(row_uuid), "type": "message",
            "role": "assistant",
            "content": _assistant_blocks(
                source, destination_provider=destination_provider,
                destination_effective_model=destination_effective_model,
                allowed_corsia_tool_names=allowed_corsia_tool_names,
                tool_argument_budget_tokens=tool_argument_budget_tokens,
            ),
        })
    if source.role != "tool":
        raise PortableHistoryError(f"unsupported Claude Code portable role {source.role!r}")
    if not source.tool_call_id:
        raise PortableHistoryError("Claude Code tool result has no tool_call_id")
    block = {
        "type": "tool_result", "tool_use_id": source.tool_call_id,
        "content": [{"type": "text", "text": _tool_result_content(
            source, thread_id=projection.thread_id,
            budget_tokens=tool_result_budget_tokens,
        )}],
    }
    if source.is_error or source.synthetic_tool_result:
        block["is_error"] = True
    return _CCProjectedMessage(
        source, "user", row_uuid, {"role": "user", "content": [block]}
    )


def project_cc_messages(
    projection: PortableHistoryProjection,
    *,
    cc_session_id: str,
    destination_provider: str | None = None,
    destination_effective_model: str | None = None,
    allowed_corsia_tool_names: frozenset[str] = frozenset(),
    tool_result_budget_tokens: int | None = None,
    tool_argument_budget_tokens: int | None = None,
) -> tuple[_CCProjectedMessage, ...]:
    """Translate a canonical projection once into Claude's exact message wire."""
    return tuple(
        _project_message(
            source, projection, cc_session_id=cc_session_id,
            destination_provider=destination_provider,
            destination_effective_model=destination_effective_model,
            allowed_corsia_tool_names=allowed_corsia_tool_names,
            tool_result_budget_tokens=tool_result_budget_tokens,
            tool_argument_budget_tokens=tool_argument_budget_tokens,
        )
        for source in projection.messages
    )


def cc_snapshot_history(
    projection: PortableHistoryProjection,
    *,
    cc_session_id: str,
    destination_provider: str | None = None,
    destination_effective_model: str | None = None,
    allowed_corsia_tool_names: frozenset[str] = frozenset(),
    tool_result_budget_tokens: int | None = None,
    tool_argument_budget_tokens: int | None = None,
) -> list[dict[str, Any]]:
    """Return the exact Claude messages represented by a portable projection."""
    return [
        item.message for item in project_cc_messages(
            projection, cc_session_id=cc_session_id,
            destination_provider=destination_provider,
            destination_effective_model=destination_effective_model,
            allowed_corsia_tool_names=allowed_corsia_tool_names,
            tool_result_budget_tokens=tool_result_budget_tokens,
            tool_argument_budget_tokens=tool_argument_budget_tokens,
        )
    ]
