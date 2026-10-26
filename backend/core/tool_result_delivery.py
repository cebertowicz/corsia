"""Persist one canonical result for a strictly validated tool execution."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import core._tool_result_delivery_support as delivery_support
from core.tool_result_projection import tool_result_integrity_metadata
from infra.delivery_states import TOOL_RESULT_BOUND, TOOL_RESULT_PROVISIONAL
from infra.queries import insert_message
from tools.tool_display import SettledResult, project_tool_display
from tools.mcp_stamp import result_stamp as mcp_result_stamp
from tools.tool_surface import dispatch_metadata


def synthetic_result_identity(
    *, tool_use_id: str, reason: str | None,
) -> tuple[str, dict[str, Any]]:
    """Return the canonical identity and metadata for a synthetic tool result."""
    metadata: dict[str, Any] = {"synthetic_tool_result": True}
    if reason:
        metadata["synthetic_reason"] = reason
    return f"synthetic:{reason or 'unknown'}:{tool_use_id}", metadata


def _reject_terminal_synthetic_result(
    *,
    db,
    thread_id: str,
    assistant_step_id: str,
    provider_tool_call_id: str | None,
) -> None:
    """Refuse a real result after terminal settlement closed its exact call."""
    if provider_tool_call_id is None:
        return
    rows = db.execute(
        "SELECT id FROM messages WHERE thread_id=? AND role='tool' "
        "AND json_valid(metadata) AND json_extract(metadata,'$.assistant_step_id')=? "
        "AND json_extract(metadata,'$.tool_call_id')=? "
        "AND json_extract(metadata,'$.synthetic_tool_result')=1",
        (thread_id, assistant_step_id, provider_tool_call_id),
    ).fetchall()
    if len(rows) > 1:
        raise delivery_support.ToolResultBindingError(
            "provider tool call has ambiguous terminal synthetic results"
        )
    if rows:
        raise delivery_support.ToolResultBindingError(
            "provider tool call is already closed by a terminal synthetic result"
        )


def _result_metadata(
    *,
    turn_id: str,
    assistant_step_id: str,
    provider_call_id: str | None,
    invocation_id: str,
    name: str,
    arguments: dict[str, Any],
    content: str,
    is_error: bool,
    extra_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    dispatch = dispatch_metadata(name, arguments)
    metadata: dict[str, Any] = {
        "turn_id": turn_id,
        "assistant_step_id": assistant_step_id,
        "tool_invocation_id": invocation_id,
        "tool_result_binding_state": (
            TOOL_RESULT_BOUND
            if provider_call_id is not None
            else TOOL_RESULT_PROVISIONAL
        ),
        "name": name,
        "arguments": arguments,
        **dispatch,
        **mcp_result_stamp(content),
        **tool_result_integrity_metadata(content),
    }
    if provider_call_id is not None:
        metadata["tool_call_id"] = provider_call_id
        metadata["provider_tool_call_id"] = provider_call_id
    if is_error:
        metadata["is_error"] = True
    if extra_metadata:
        protected = set(metadata).intersection(extra_metadata)
        if protected:
            raise delivery_support.ToolResultBindingError(
                "extra tool-result metadata overrides canonical fields: "
                f"{sorted(protected)}"
            )
        metadata.update(extra_metadata)
    return metadata


def _stamp_browser_chip(
    db, *, thread_id: str, assistant_step_id: str, name: str, arguments: dict[str, Any],
    content: str, metadata: dict[str, Any],
) -> None:
    """Store a visible open's chip on its Assistant Step, once, as the result settles.

    The display projection decides; its answer is kept on the Step so every reader
    (a Focus run stub in SQL, a reloaded row, a finalized live Step) reads one fact.
    A later open in the same Step replaces it.
    """
    chip = project_tool_display(name, arguments, SettledResult.from_row(content, metadata)).browser_chip
    if chip is None:
        return
    db.execute(
        "UPDATE messages SET metadata=json_set(metadata, '$.browser_chip', json(?)) "
        "WHERE id=? AND thread_id=? AND role='assistant' AND json_valid(metadata)",
        (json.dumps(chip), assistant_step_id, thread_id),
    )


def _insert_exact_result(
    *,
    db,
    thread_id: str,
    exact_message_id: str,
    metadata: dict[str, Any],
    content: str,
    turn_id: str,
    assistant_step_id: str,
    invocation_id: str,
    provider_tool_call_id: str | None,
    name: str,
    arguments: dict[str, Any],
    is_error: bool,
) -> delivery_support.ToolExecutionResult:
    try:
        insert_message(
            thread_id,
            "tool",
            content,
            metadata,
            msg_id=exact_message_id,
            db=db,
        )
    except sqlite3.IntegrityError:
        existing = delivery_support.existing_result(db, exact_message_id)
        if existing is None:
            raise
        return delivery_support.require_same_result(
            existing,
            thread_id=thread_id,
            turn_id=turn_id,
            assistant_step_id=assistant_step_id,
            invocation_id=invocation_id,
            provider_tool_call_id=provider_tool_call_id,
            name=name,
            arguments=arguments,
            content=content,
            is_error=is_error,
            db=db,
        )
    _stamp_browser_chip(
        db, thread_id=thread_id, assistant_step_id=assistant_step_id, name=name,
        arguments=arguments, content=content, metadata=metadata,
    )
    persisted = delivery_support.existing_result(db, exact_message_id)
    if persisted is None:
        raise delivery_support.ToolResultBindingError(
            "canonical tool result was not persisted"
        )
    return persisted


def _resolve_provider_binding(
    *,
    db,
    thread_id: str,
    turn_id: str,
    assistant_step_id: str,
    provider_tool_call_id: str | None,
    name: str,
    arguments: dict[str, Any] | None,
    verified_metadata: object | None,
) -> tuple[str | None, dict[str, Any]]:
    if verified_metadata is None:
        return delivery_support.select_provider_call(
            db=db,
            thread_id=thread_id,
            assistant_step_id=assistant_step_id,
            provider_tool_call_id=provider_tool_call_id,
            name=name,
            arguments=arguments,
        )
    projected = delivery_support.metadata_object(verified_metadata)
    if projected.get("turn_id") != turn_id:
        raise delivery_support.ToolResultBindingError(
            "verified Assistant Step projection changed Turn ownership"
        )
    return delivery_support.select_provider_call_from_metadata(
        metadata=projected,
        assistant_step_id=assistant_step_id,
        provider_tool_call_id=provider_tool_call_id,
        name=name,
        arguments=arguments,
    )


def _persist_resolved_result(
    *,
    db,
    thread_id: str,
    turn_id: str,
    exact_message_id: str,
    assistant_step_id: str,
    invocation_id: str,
    provider_tool_call_id: str | None,
    name: str,
    arguments: dict[str, Any],
    content: str,
    is_error: bool,
    extra_metadata: dict[str, Any] | None,
) -> delivery_support.ToolExecutionResult:
    """Write the one already-validated, non-terminal tool result."""
    metadata = _result_metadata(
        turn_id=turn_id,
        assistant_step_id=assistant_step_id,
        provider_call_id=provider_tool_call_id,
        invocation_id=invocation_id,
        name=name,
        arguments=arguments,
        content=content,
        is_error=is_error,
        extra_metadata=extra_metadata,
    )
    return _insert_exact_result(
        db=db,
        thread_id=thread_id,
        exact_message_id=exact_message_id,
        metadata=metadata,
        content=content,
        turn_id=turn_id,
        assistant_step_id=assistant_step_id,
        invocation_id=invocation_id,
        provider_tool_call_id=provider_tool_call_id,
        name=name,
        arguments=arguments,
        is_error=is_error,
    )


def persist_tool_execution_result(
    *,
    thread_id: str,
    turn_id: str,
    invocation_id: str,
    assistant_step_id: str | None,
    name: str,
    arguments: dict[str, Any] | None,
    content: str,
    is_error: bool,
    provider_tool_call_id: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
    _verified_assistant_step_metadata: object | None = None,
    db,
) -> delivery_support.ToolExecutionResult:
    """Persist exact external execution bytes under proven call lineage."""
    validated = delivery_support.validate_execution_request(
        thread_id=thread_id,
        turn_id=turn_id,
        invocation_id=invocation_id,
        assistant_step_id=assistant_step_id,
        name=name,
        arguments=arguments,
        content=content,
    )
    thread_id, turn_id, invocation_id, assistant_step_id, name = validated
    exact_message_id = delivery_support.message_id(
        thread_id, assistant_step_id, invocation_id
    )
    provider_call_id, bound_arguments = _resolve_provider_binding(
        db=db,
        thread_id=thread_id,
        turn_id=turn_id,
        assistant_step_id=assistant_step_id,
        provider_tool_call_id=provider_tool_call_id,
        name=name,
        arguments=arguments,
        verified_metadata=_verified_assistant_step_metadata,
    )
    existing = delivery_support.existing_result(db, exact_message_id)
    if existing is not None:
        return delivery_support.require_same_result(
            existing,
            thread_id=thread_id,
            turn_id=turn_id,
            assistant_step_id=assistant_step_id,
            invocation_id=invocation_id,
            provider_tool_call_id=provider_call_id,
            name=name,
            arguments=bound_arguments,
            content=content,
            is_error=is_error,
            db=db,
        )
    _reject_terminal_synthetic_result(
        db=db,
        thread_id=thread_id,
        assistant_step_id=assistant_step_id,
        provider_tool_call_id=provider_call_id,
    )
    return _persist_resolved_result(
        db=db,
        thread_id=thread_id,
        turn_id=turn_id,
        exact_message_id=exact_message_id,
        assistant_step_id=assistant_step_id,
        invocation_id=invocation_id,
        provider_tool_call_id=provider_call_id,
        name=name,
        arguments=bound_arguments,
        content=content,
        is_error=is_error,
        extra_metadata=extra_metadata,
    )
