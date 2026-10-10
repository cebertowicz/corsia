"""Stateless Claude transcript record proof for checkpoint boundaries."""

from __future__ import annotations

import json

from history.types import DurableBoundaryMessage as ClaudeTranscriptBoundary


def _provider_row_identities(
    boundary: ClaudeTranscriptBoundary,
    provider_session_id: str | None,
) -> tuple[set[str], set[str]]:
    """Return exact row/message ids a live or reconstructed transcript may use.

    A transcript Claude wrote names an assistant row only by Claude's own
    ``message.id``; one CORSIA reconstructed (``cc_transcript_projection``)
    names it by the row uuid and its derived ``msg_corsia_*`` id.
    """
    row_ids = {boundary.message_id} if boundary.message_id else set()
    message_ids = set(row_ids)
    if boundary.role == "assistant" and boundary.provider_message_id:
        message_ids.add(boundary.provider_message_id)
    if provider_session_id and boundary.message_id:
        from subagents.cc_transcript_projection import (
            _assistant_message_id,
            _stable_uuid,
        )

        row_uuid = _stable_uuid(
            provider_session_id,
            boundary.message_id,
            boundary.role,
        )
        row_ids.add(row_uuid)
        if boundary.role == "assistant":
            message_ids.add(_assistant_message_id(row_uuid))
    return row_ids, message_ids


def _matches_boundary(
    record: dict,
    boundary: ClaudeTranscriptBoundary,
    provider_session_id: str | None = None,
) -> bool:
    """Prove one transcript record is the exact durable row/group recorded through."""
    message = record.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    row_ids, message_ids = _provider_row_identities(boundary, provider_session_id)
    if boundary.role == "assistant":
        return (
            record.get("type") == "assistant"
            and bool(message_ids)
            and (
                message.get("id") in message_ids
                or record.get("uuid") in row_ids
            )
        )
    if boundary.role == "user":
        return (
            record.get("type") == "user"
            and bool(row_ids)
            and record.get("uuid") in row_ids
        )
    if boundary.role != "tool" or not boundary.tool_call_id or not isinstance(content, list):
        return False
    matches = [
        block
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "tool_result"
        and block.get("tool_use_id") == boundary.tool_call_id
    ]
    # Call identity is already bound to the canonical result. Provider-specific
    # projection may add media labels or CORSIA's invocation marker, so text is
    # deliberately not an identity field here.
    return len(matches) == 1


def _semantic_key(record: dict) -> tuple[str, object] | None:
    message = record.get("message")
    if not isinstance(message, dict) or record.get("type") not in {"assistant", "user"}:
        return None
    if record.get("type") == "assistant":
        message_id = message.get("id")
        return ("assistant", message_id) if isinstance(message_id, str) else None
    content = message.get("content")
    if isinstance(content, list):
        result_ids = tuple(
            block.get("tool_use_id")
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_result"
        )
        if result_ids:
            return "tool_results", result_ids
    row_uuid = record.get("uuid")
    return ("user", row_uuid) if isinstance(row_uuid, str) else None


def _assistant_message(record: dict) -> tuple[str, list] | None:
    """Validate the Claude assistant shape needed by both streaming passes."""
    if record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    message_id = message.get("id")
    content = message.get("content")
    if not isinstance(message_id, str) or not message_id or not isinstance(content, list):
        return None
    return message_id, content


def _assistant_tool_ids(record: dict) -> tuple[str, ...]:
    assistant = _assistant_message(record)
    if assistant is None:
        return ()
    return tuple(
        str(block["id"])
        for block in assistant[1]
        if isinstance(block, dict)
        and block.get("type") == "tool_use"
        and isinstance(block.get("id"), str)
        and block["id"]
    )


def _tool_result_ids(record: dict) -> tuple[str, ...]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return ()
    return tuple(
        str(block["tool_use_id"])
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "tool_result"
        and isinstance(block.get("tool_use_id"), str)
        and block["tool_use_id"]
    )


def _tool_group_proved(
    boundary: ClaudeTranscriptBoundary,
    assistant_groups: dict[str, set[str]],
    result_ids: set[str],
) -> bool:
    if boundary.role != "tool":
        return True
    if not boundary.tool_call_ids:
        return bool(boundary.tool_call_id and boundary.tool_call_id in result_ids)
    required = set(boundary.tool_call_ids)
    if not required or not required.issubset(result_ids):
        return False
    return sum(calls == required for calls in assistant_groups.values()) == 1


def _trailing_batch_results(
    trailing: set[str] | None,
    record: dict,
    batch: frozenset[str],
    assistant_groups: dict[str, set[str]],
) -> set[str] | None:
    """Track the batch results written since the batch's own assistant message.

    Claude writes one line per tool result of a parallel batch, in its own
    order, which need not match the order CORSIA saved them. The cut holds
    when every line after the batch's assistant message is one of its results.
    """
    if not batch:
        return None
    assistant = _assistant_message(record)
    if assistant is not None:
        return set() if assistant_groups.get(assistant[0], set()) <= batch else None
    results = set(_tool_result_ids(record))
    if trailing is None or not results or not results <= batch:
        return None
    return trailing | results


def _json_record(line: bytes) -> dict | None:
    """Decode one transcript line or fail the entire boundary proof closed."""
    try:
        record = json.loads(line)
    except (TypeError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _current_tool_message(stream, tool_call_id: str) -> str | None:
    """Prove exactly one assistant record owns the still-live tool call."""
    current_message_id = None
    tool_matches = 0
    for line in stream:
        record = _json_record(line)
        if record is None:
            return None
        assistant = _assistant_message(record)
        if record.get("type") == "assistant" and assistant is None:
            return None
        if assistant is None:
            continue
        message_id, content = assistant
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("id") == tool_call_id
            ):
                tool_matches += 1
                current_message_id = message_id
    return current_message_id if tool_matches == 1 else None


def _prefix_before_current_message(
    stream,
    current_message_id: str,
    boundary: ClaudeTranscriptBoundary,
    provider_session_id: str | None = None,
) -> int | str:
    """Find the first current-step record while retaining only boundary facts.

    Returns the prefix length, or the content-free check that failed.
    """
    boundary_matches = 0
    last_semantic_key = None
    boundary_key = None
    assistant_groups: dict[str, set[str]] = {}
    result_ids: set[str] = set()
    batch = (
        frozenset(boundary.tool_call_ids)
        if boundary.role == "tool" and boundary.tool_call_id in boundary.tool_call_ids
        else frozenset()
    )
    trailing_batch: set[str] | None = None
    offset = 0
    role = boundary.role
    for line in stream:
        record = _json_record(line)
        if record is None:
            return "transcript_invalid"
        assistant = _assistant_message(record)
        if record.get("type") == "assistant" and assistant is None:
            return "transcript_invalid"
        if assistant is not None and assistant[0] == current_message_id:
            through_boundary = boundary_key == last_semantic_key or (
                bool(batch) and trailing_batch == batch
            )
            if boundary_matches == 0:
                return f"boundary_not_found:{role}"
            if boundary_matches > 1:
                return f"boundary_ambiguous:{role}"
            if not through_boundary:
                return f"not_through_boundary:{role}"
            if not _tool_group_proved(boundary, assistant_groups, result_ids):
                return f"group_unproved:{role}"
            return offset if offset else "empty_prefix"
        if record.get("type") in {"assistant", "user"}:
            if assistant is not None:
                assistant_groups.setdefault(assistant[0], set()).update(
                    _assistant_tool_ids(record)
                )
            result_ids.update(_tool_result_ids(record))
            semantic_key = _semantic_key(record)
            if semantic_key is not None:
                last_semantic_key = semantic_key
                trailing_batch = _trailing_batch_results(
                    trailing_batch, record, batch, assistant_groups,
                )
            if _matches_boundary(record, boundary, provider_session_id):
                if semantic_key == boundary_key:
                    offset += len(line)
                    continue
                boundary_matches += 1
                boundary_key = semantic_key
        offset += len(line)
    return "current_message_missing"


def tool_prefix_from_stream(
    stream,
    tool_call_id: str,
    boundary: ClaudeTranscriptBoundary,
    provider_session_id: str | None = None,
) -> int | str:
    """Prove the durable boundary with two bounded-memory transcript passes.

    Returns the prefix length, or the one reason no prefix was proved.
    """
    current_message_id = _current_tool_message(stream, tool_call_id)
    if current_message_id is None:
        return "current_call_not_found"
    stream.seek(0)
    return _prefix_before_current_message(
        stream,
        current_message_id,
        boundary,
        provider_session_id,
    )


def _boundary_group_cut(
    offset: int,
    matches: int,
    boundary: ClaudeTranscriptBoundary,
    assistant_groups: dict[str, set[str]],
    result_ids: set[str],
) -> int | str:
    if matches == 0:
        return f"boundary_not_found:{boundary.role}"
    if matches > 1:
        return f"boundary_ambiguous:{boundary.role}"
    if not _tool_group_proved(boundary, assistant_groups, result_ids):
        return f"group_unproved:{boundary.role}"
    return offset


def boundary_prefix_from_stream(
    stream,
    boundary: ClaudeTranscriptBoundary,
    provider_session_id: str | None,
) -> int | None:
    """Cut after one exact boundary group and its non-semantic bookkeeping."""
    cut = boundary_prefix_cut(stream, boundary, provider_session_id)
    return cut if isinstance(cut, int) else None


def boundary_prefix_cut(
    stream,
    boundary: ClaudeTranscriptBoundary,
    provider_session_id: str | None,
) -> int | str:
    """Like ``boundary_prefix_from_stream``, naming the failed check."""
    matched_key = None
    matches = 0
    offset = 0
    assistant_groups: dict[str, set[str]] = {}
    result_ids: set[str] = set()
    for line in stream:
        record = _json_record(line)
        if record is None:
            return "transcript_invalid"
        assistant = _assistant_message(record)
        if record.get("type") == "assistant" and assistant is None:
            return "transcript_invalid"
        semantic_key = _semantic_key(record)
        if semantic_key is not None and matched_key is not None and semantic_key != matched_key:
            return _boundary_group_cut(
                offset, matches, boundary, assistant_groups, result_ids,
            )
        if assistant is not None:
            assistant_groups.setdefault(assistant[0], set()).update(
                _assistant_tool_ids(record)
            )
        result_ids.update(_tool_result_ids(record))
        if _matches_boundary(record, boundary, provider_session_id):
            if semantic_key != matched_key:
                matches += 1
                matched_key = semantic_key
        offset += len(line)
    if matched_key is None:
        return f"boundary_not_found:{boundary.role}"
    return _boundary_group_cut(
        offset, matches, boundary, assistant_groups, result_ids,
    )
