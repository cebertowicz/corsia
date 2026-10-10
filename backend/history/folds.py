"""Assistant-Step fold units and bounded transport fragments."""

from __future__ import annotations

import json
from typing import Any

from history.turns import group_assistant_steps
from history.types import (
    CheckpointFoldFragment,
    CheckpointFoldUnit,
    PortableHistoryError,
    PortableHistoryProjection,
    PortableMessage,
)


def checkpoint_fold_units(
    projection: PortableHistoryProjection,
) -> tuple[CheckpointFoldUnit, ...]:
    """Project canonical Turns into flat, ordered Assistant Step fold units."""
    units: list[CheckpointFoldUnit] = []
    for turn in projection.turns:
        grouped = group_assistant_steps(turn.messages)

        for group_index, messages in enumerate(grouped):
            completes_turn = group_index == len(grouped) - 1
            units.append(
                CheckpointFoldUnit(
                    turn_id=turn.turn_id,
                    status=turn.status,
                    legacy_reason=turn.legacy_reason,
                    ordinal=len(units),
                    messages=messages,
                    completes_turn=completes_turn,
                    boundary=turn.boundary if completes_turn else None,
                )
            )
    return tuple(units)


def _split_checkpoint_payload(text: str, max_payload_bytes: int) -> tuple[str, ...]:
    if not text:
        return ("",)
    parts: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for character in text:
        character_bytes = len(character.encode("utf-8"))
        if character_bytes > max_payload_bytes:
            raise ValueError("max_payload_bytes cannot hold one UTF-8 character")
        if current and current_bytes + character_bytes > max_payload_bytes:
            parts.append("".join(current))
            current = []
            current_bytes = 0
        current.append(character)
        current_bytes += character_bytes
    if current:
        parts.append("".join(current))
    return tuple(parts)


def _fold_payload(
    message: PortableMessage,
    *,
    payload_kind: str,
    text: str,
    assistant_step_id: str | None = None,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
    reasoning_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "payload_kind": payload_kind,
        "message": message,
        "text": text,
        "assistant_step_id": assistant_step_id,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "reasoning_provenance": reasoning_provenance or {},
    }


def _reasoning_payload(
    message: PortableMessage,
    *,
    max_payload_bytes: int,
) -> dict[str, Any] | None:
    if message.reasoning_text is None and not message.reasoning_provenance:
        return None
    try:
        provenance_bytes = len(
            json.dumps(
                message.reasoning_provenance,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise PortableHistoryError(
            f"assistant reasoning provenance for {message.message_id!r} is not portable"
        ) from exc
    if provenance_bytes > max_payload_bytes:
        raise PortableHistoryError(
            f"assistant reasoning provenance for {message.message_id!r} "
            "exceeds the fragment metadata bound"
        )
    return _fold_payload(
        message,
        payload_kind="assistant_reasoning",
        text=message.reasoning_text or "",
        assistant_step_id=message.message_id,
        reasoning_provenance=dict(message.reasoning_provenance),
    )


def _tool_arguments_payload(
    message: PortableMessage,
    tool_call: dict[str, Any],
) -> dict[str, Any]:
    if set(tool_call) - {"id", "type", "function"}:
        raise PortableHistoryError(
            f"tool call {tool_call.get('id')!r} has unsupported fragment metadata"
        )
    if tool_call.get("type") != "function":
        raise PortableHistoryError(
            f"tool call {tool_call.get('id')!r} is not a portable function"
        )
    function = tool_call.get("function")
    if not isinstance(function, dict) or set(function) - {"name", "arguments"}:
        raise PortableHistoryError(
            f"tool call {tool_call.get('id')!r} has unsupported function metadata"
        )
    tool_name = function.get("name")
    if not isinstance(tool_name, str) or not tool_name:
        raise PortableHistoryError(
            f"tool call {tool_call.get('id')!r} has no portable name"
        )
    arguments: Any = function.get("arguments", "")
    if not isinstance(arguments, str):
        arguments = json.dumps(
            arguments,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    return _fold_payload(
        message,
        payload_kind="tool_arguments",
        text=arguments,
        assistant_step_id=message.message_id,
        tool_call_id=str(tool_call["id"]),
        tool_name=tool_name,
    )


def _assistant_payloads(
    message: PortableMessage,
    *,
    max_payload_bytes: int,
) -> list[dict[str, Any]]:
    payloads = [
        _fold_payload(
            message,
            payload_kind="assistant_text",
            text=message.content,
            assistant_step_id=message.message_id,
        )
    ]
    reasoning = _reasoning_payload(message, max_payload_bytes=max_payload_bytes)
    if reasoning is not None:
        payloads.append(reasoning)
    payloads.extend(
        _tool_arguments_payload(message, tool_call) for tool_call in message.tool_calls
    )
    return payloads


def _unit_payloads(
    unit: CheckpointFoldUnit,
    *,
    max_payload_bytes: int,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for message in unit.messages:
        if message.role == "user":
            payloads.append(
                _fold_payload(message, payload_kind="user_text", text=message.content)
            )
        elif message.role == "assistant":
            payloads.extend(
                _assistant_payloads(message, max_payload_bytes=max_payload_bytes)
            )
        elif message.role == "tool":
            payloads.append(
                _fold_payload(
                    message,
                    payload_kind="tool_result",
                    text=message.content,
                    assistant_step_id=message.assistant_step_id,
                    tool_call_id=message.tool_call_id,
                    tool_name=message.tool_name,
                )
            )
        else:
            raise PortableHistoryError(
                f"unsupported checkpoint fold role {message.role!r}"
            )
    return payloads


def fragment_checkpoint_fold_unit(
    unit: CheckpointFoldUnit,
    *,
    max_payload_bytes: int,
) -> tuple[CheckpointFoldFragment, ...]:
    """Split independently oversized fold payloads without changing identity."""
    if (
        isinstance(max_payload_bytes, bool)
        or not isinstance(max_payload_bytes, int)
        or max_payload_bytes <= 0
    ):
        raise ValueError("max_payload_bytes must be a positive integer")

    payloads = _unit_payloads(unit, max_payload_bytes=max_payload_bytes)

    fragments: list[CheckpointFoldFragment] = []
    metadata_emitted_message_ids: set[str] = set()
    for payload in payloads:
        message = payload["message"]
        text = payload["text"]
        parts = _split_checkpoint_payload(text, max_payload_bytes)
        for part_index, part in enumerate(parts, start=1):
            preserves_message_metadata = (
                message.message_id not in metadata_emitted_message_ids
            )
            fragments.append(
                CheckpointFoldFragment(
                    unit_ordinal=unit.ordinal,
                    turn_id=unit.turn_id,
                    turn_status=unit.status,
                    turn_legacy_reason=unit.legacy_reason,
                    payload_kind=payload["payload_kind"],
                    message_id=message.message_id,
                    message_seq=message.seq,
                    message_created_at=message.created_at,
                    role=message.role,
                    assistant_step_id=payload["assistant_step_id"],
                    tool_call_id=payload["tool_call_id"],
                    tool_name=payload["tool_name"],
                    is_error=message.is_error,
                    synthetic_tool_result=message.synthetic_tool_result,
                    synthetic_reason=message.synthetic_reason,
                    reasoning_provenance=payload["reasoning_provenance"],
                    artifact_references=(
                        message.artifact_references
                        if preserves_message_metadata
                        else ()
                    ),
                    inbound_source=(
                        dict(message.inbound_source)
                        if preserves_message_metadata
                        else {}
                    ),
                    part_index=part_index,
                    part_count=len(parts),
                    text=part,
                    byte_count=len(part.encode("utf-8")),
                )
            )
            metadata_emitted_message_ids.add(message.message_id)
    return tuple(fragments)
