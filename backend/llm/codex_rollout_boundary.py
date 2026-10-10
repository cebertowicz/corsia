"""Prove one exact Codex rollout boundary without loading the rollout."""

from __future__ import annotations

import json
from typing import Any, BinaryIO, Iterator

from history.types import DurableBoundaryMessage as CodexRolloutBoundary
from llm.isolated_structured import _strict_json_loads


def _payload(record: object) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else None


def _response_item(record: object, *payload_types: str) -> bool:
    payload = _payload(record)
    return (
        isinstance(record, dict)
        and record.get("type") == "response_item"
        and payload is not None
        and (not payload_types or payload.get("type") in payload_types)
    )


def _token_count(record: object) -> bool:
    if not isinstance(record, dict) or record.get("type") != "event_msg":
        return False
    payload = _payload(record)
    info = payload.get("info") if payload is not None else None
    total = info.get("total_token_usage") if isinstance(info, dict) else None
    if not isinstance(total, dict) or payload is None or payload.get("type") != "token_count":
        return False
    from llm.codex_rollout_files import _RESTORED_TOKEN_FIELDS

    return all(
        type(total.get(field)) is int and total[field] >= 0
        for field in _RESTORED_TOKEN_FIELDS
    )


def _is_token_count(record: object) -> bool:
    payload = _payload(record)
    return (
        isinstance(record, dict)
        and record.get("type") == "event_msg"
        and payload is not None
        and payload.get("type") == "token_count"
    )


def _item_text(item: dict[str, Any]) -> str | None:
    item_type = item.get("type")
    if item_type == "agentMessage":
        text = item.get("text")
        return text if isinstance(text, str) else None
    if item_type != "userMessage":
        return None
    content = item.get("content")
    if not isinstance(content, list) or any(
        not isinstance(part, dict)
        or part.get("type") != "text"
        or not isinstance(part.get("text"), str)
        for part in content
    ):
        return None
    return "".join(part["text"] for part in content)


def _rollout_message_id(boundary: CodexRolloutBoundary) -> str | None:
    """The one id the rollout names this row by.

    Codex writes an assistant message's item id on both its ``item_completed``
    and its ``response_item`` record, so a row carrying that id is proven by it
    alone and never by CORSIA's row id.
    """
    if boundary.provider_message_id:
        return boundary.provider_message_id
    return boundary.message_id or None


# The rollout writes PascalCase item types; the live protocol uses camelCase.
_ITEM_TYPES = {
    "assistant": {"agentMessage", "AgentMessage"},
    "user": {"userMessage", "UserMessage"},
}


def _item_ids(item: dict[str, Any], role: str) -> tuple[object, ...]:
    """Ids a completed item names its row by.

    A user item also carries the ``clientUserMessageId`` CORSIA sent, which is
    the durable anchor row id (``client_id`` in the rollout).
    """
    if role != "user":
        return (item.get("id"),)
    return (item.get("id"), item.get("client_id"), item.get("clientId"))


def _message_matches(record: object, boundary: CodexRolloutBoundary) -> bool:
    expected_id = _rollout_message_id(boundary)
    if expected_id is None:
        return False
    payload = _payload(record)
    item = payload.get("item") if payload is not None else None
    if (
        isinstance(record, dict)
        and record.get("type") == "event_msg"
        and payload is not None
        and payload.get("type") == "item_completed"
        and isinstance(item, dict)
        and item.get("type") in _ITEM_TYPES.get(boundary.role, ())
        and expected_id in _item_ids(item, boundary.role)
    ):
        return True
    if not _response_item(record, "message") or payload is None:
        return False
    return payload.get("role") == boundary.role and payload.get("id") == expected_id


_ROLLOUT_TAIL_SCAN_BYTES = 4 * 1024 * 1024
_ROLLOUT_TAIL_BLOCK_BYTES = 16 * 1024


def _tool_output_matches(record: object, call_id: str) -> bool:
    payload = _payload(record)
    return (
        _response_item(record, "custom_tool_call_output", "function_call_output")
        and payload is not None
        and payload.get("call_id") == call_id
    )


def _rollout_record(line: bytes) -> dict[str, Any] | None:
    try:
        record = _strict_json_loads(line)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _reverse_tail_records(stream: BinaryIO) -> Iterator[tuple[dict[str, Any], int]]:
    """Yield complete JSONL records backwards from a bounded rollout tail."""
    stream.seek(0, 2)
    end = stream.tell()
    floor = max(0, end - _ROLLOUT_TAIL_SCAN_BYTES)
    partial_first_line = False
    if floor:
        stream.seek(floor - 1)
        partial_first_line = stream.read(1) != b"\n"

    cursor = end
    carry = b""
    while cursor > floor:
        start = max(floor, cursor - _ROLLOUT_TAIL_BLOCK_BYTES)
        stream.seek(start)
        chunk = stream.read(cursor - start)
        lines = (chunk + carry).splitlines(keepends=True)
        discard_first = start > floor or partial_first_line
        discarded = lines.pop(0) if discard_first and lines else b""
        carry = discarded
        offset = start + len(discarded)
        records: list[tuple[dict[str, Any], int]] = []
        for line in lines:
            record = _rollout_record(line)
            if record is None:
                raise ValueError("rollout tail contains an invalid record")
            records.append((record, offset))
            offset += len(line)
        yield from reversed(records)
        cursor = start


def _expected_tool_item(record: object, boundary: CodexRolloutBoundary) -> bool:
    payload = _payload(record)
    item = payload.get("item") if payload is not None else None
    return (
        boundary.role == "tool"
        and isinstance(record, dict)
        and record.get("type") == "event_msg"
        and payload is not None
        and payload.get("type") == "item_completed"
        and isinstance(item, dict)
        and item.get("id") == boundary.tool_call_id
    )


def _response_call_id(record: object) -> str | None:
    payload = _payload(record)
    call_id = payload.get("call_id") if payload is not None else None
    return (
        call_id
        if _response_item(record, "custom_tool_call", "function_call")
        and isinstance(call_id, str)
        and call_id
        else None
    )


def _is_token_usage_record(record: object) -> bool:
    return isinstance(record, dict) and record.get("type") == "token_usage_record"


def _cut_after_boundary(
    following: list[tuple[dict[str, Any], int]],
    boundary: CodexRolloutBoundary,
    call_id: str | None = None,
) -> int | str:
    if call_id is not None:
        if not following or not _tool_output_matches(following[0][0], call_id):
            return "tool_output_missing"
        following = following[1:]

    receipt_seen = False
    for record, offset in following:
        if not receipt_seen and _is_token_usage_record(record):
            continue
        if _is_token_count(record):
            if not _token_count(record):
                return f"receipt_invalid:{boundary.role}"
            receipt_seen = True
            continue
        if not receipt_seen:
            return f"receipt_missing:{boundary.role}"
        return offset
    return f"receipt_missing:{boundary.role}"


def codex_rollout_boundary_cut(
    stream: BinaryIO,
    boundary: CodexRolloutBoundary,
) -> int | str:
    """Return a proven bounded-tail prefix, or the reason it stays cold.

    Reasons are content-free: ``boundary_invalid``,
    ``boundary_outside_tail:<role>``, ``boundary_not_found:<role>``,
    ``receipt_missing:<role>``, ``receipt_invalid:<role>``,
    ``tool_output_missing``, ``rollout_tail_invalid``, ``rollout_unreadable``.
    """
    if (
        boundary.role not in {"user", "assistant", "tool"}
        or not isinstance(boundary.content, str)
        or (boundary.role == "tool" and not boundary.tool_call_id)
        or (boundary.role != "tool" and not boundary.message_id)
    ):
        return "boundary_invalid"

    following: list[tuple[dict[str, Any], int]] = []
    waiting_for_call_id = False
    try:
        stream.seek(0, 2)
        boundary_outside_tail = stream.tell() > _ROLLOUT_TAIL_SCAN_BYTES
        for record, offset in _reverse_tail_records(stream):
            if waiting_for_call_id:
                if _response_item(record):
                    return _cut_after_boundary(
                        list(reversed(following)), boundary, _response_call_id(record)
                    )
                continue
            if boundary.role == "tool" and _expected_tool_item(record, boundary):
                waiting_for_call_id = True
                continue
            if boundary.role != "tool" and _message_matches(record, boundary):
                return _cut_after_boundary(list(reversed(following)), boundary)
            following.append((record, offset))
    except OSError:
        return "rollout_unreadable"
    except ValueError:
        return "rollout_tail_invalid"
    if boundary_outside_tail:
        return f"boundary_outside_tail:{boundary.role}"
    return f"boundary_not_found:{boundary.role}"


def codex_rollout_boundary_prefix_bytes(
    stream: BinaryIO,
    boundary: CodexRolloutBoundary,
) -> int | None:
    """Return a proven bounded-tail prefix; unproven evidence stays cold."""
    cut = codex_rollout_boundary_cut(stream, boundary)
    return cut if isinstance(cut, int) else None
