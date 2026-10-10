"""Codex effective-context reconstruction helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from core.effective_context import ContextSourceEntry, EffectiveContextSnapshot
from core.installed_context import InstalledDelivery
from core import dynamic_context_registry
from core.message_history import frame_agent_envelope
from history.types import (
    HistoryBoundary,
    PortableHistoryProjection,
    PortableMessage,
)
from subagents.codex_context_sources import (
    CONTEXT_UNSET as _CONTEXT_UNSET,
    append_history_sources as _append_codex_history_sources,
    build_reconstruction_snapshot as _build_reconstruction_snapshot,
    context_sources as _codex_context_sources,
)
from tools.catalog import normalize_tool_name


@dataclass(frozen=True)
class CodexContextReconstruction:
    task_text: str
    turn_text: str
    snapshot: EffectiveContextSnapshot
    history_format_version: int
    history_start_exclusive: HistoryBoundary | None
    history_sealed_boundary: HistoryBoundary | None
    history_full_raw_replay_available: bool
    history_items: tuple[dict[str, Any], ...] = ()
    # The exact checkpoint text placed first in ``history_items``, if any.
    checkpoint_text: str | None = None
    # Private InstalledDelivery the dispatch retains; never published.
    installed: InstalledDelivery | None = field(
        default=None, compare=False, repr=False,
    )

    @property
    def snapshot_id(self) -> str:
        return self.snapshot.snapshot_id

    @property
    def content_hash(self) -> str:
        return self.snapshot.content_hash


def _append_json_section(text: str, label: str, value: Any) -> str:
    section = f"{label}\n{json.dumps(value, sort_keys=True, default=str)}"
    return f"{text}\n\n{section}" if text else section


def _portable_text(
    message: PortableMessage,
    *,
    codex_thread_id: str,
    tool_result_budget_tokens: int | None,
) -> str:
    if message.role == "tool":
        from core.tool_result_projection import render_tool_result_for_model

        text = render_tool_result_for_model(
            codex_thread_id,
            message,
            budget_tokens=tool_result_budget_tokens,
        )
    else:
        text = (
            frame_agent_envelope(message.content, message.inbound_source)
            if message.role == "user"
            else message.content
        )
    if message.artifact_references:
        text = _append_json_section(
            text,
            "[artifact references]",
            list(message.artifact_references),
        )
    return text


def codex_inject_items(
    projection: PortableHistoryProjection,
    *,
    tool_result_budget_tokens: int | None = None,
    tool_argument_budget_tokens: int | None = None,
) -> tuple[dict[str, Any], ...]:
    """Translate one canonical projection to Codex's proven message shape.

    Tools remain labelled text because typed tool replay is unproved. The
    selected order, retention markers, and renderer accounting stay exact.
    """
    items: list[dict[str, Any]] = []
    for message in projection.messages:
        role = message.role
        if role == "user":
            items.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": _portable_text(
                                message,
                                codex_thread_id=projection.thread_id,
                                tool_result_budget_tokens=tool_result_budget_tokens,
                            ),
                        }
                    ],
                }
            )
        elif role == "assistant":
            text = _portable_text(
                message,
                codex_thread_id=projection.thread_id,
                tool_result_budget_tokens=tool_result_budget_tokens,
            )
            if message.tool_calls:
                text = _append_json_section(
                    text,
                    "[assistant tool calls]",
                    _project_assistant_calls(
                        message,
                        tool_argument_budget_tokens,
                    ),
                )
            items.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            )
        elif role == "tool":
            call_id = message.tool_call_id or "unknown"
            tool_name = normalize_tool_name(message.tool_name)
            label = (
                f"{tool_name}; call_id={call_id}"
                if tool_name
                else f"call_id={call_id}"
            )
            tool_text = _portable_text(
                message,
                codex_thread_id=projection.thread_id,
                tool_result_budget_tokens=tool_result_budget_tokens,
            )
            items.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"[tool result: {label}]\n{tool_text}",
                        }
                    ],
                }
            )
    return tuple(items)


def _project_assistant_calls(message, budget_tokens: int | None) -> list[dict]:
    from history.tool_argument_projection import project_checkpoint_tool_call

    projected_calls = []
    for call in message.tool_calls:
        projected = project_checkpoint_tool_call(
            message.message_id, call, budget_tokens,
        )
        function = projected.get("function")
        if isinstance(function, dict):
            projected["function"] = {
                **function,
                "name": normalize_tool_name(function.get("name")),
            }
        projected_calls.append(projected)
    return projected_calls


def _boundary_payload(boundary: HistoryBoundary | None) -> dict[str, Any] | None:
    if boundary is None:
        return None
    return {
        "thread_id": boundary.thread_id,
        "history_epoch": boundary.history_epoch,
        "seq": boundary.seq,
        "message_id": boundary.message_id,
    }


def _checkpoint_history_items(
    checkpoint_text: str | None,
) -> tuple[dict[str, Any], ...]:
    if not checkpoint_text:
        return ()
    return (
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": checkpoint_text}],
        },
    )


def _portable_history_source(
    projection: PortableHistoryProjection,
) -> dict[str, Any]:
    return {
        "format_version": projection.format_version,
        "start_exclusive": _boundary_payload(projection.start_exclusive),
        "sealed_boundary": _boundary_payload(projection.sealed_boundary),
        "full_raw_replay_available": projection.full_raw_replay_available,
    }


def _render_codex_context_text(
    snapshot: EffectiveContextSnapshot,
    *,
    user_text: str,
    agent_source: ContextSourceEntry | None,
) -> tuple[str, str]:
    """Render one full thread seed and the ordinary per-turn input."""
    excluded = {"codex-user-task"}
    if agent_source is not None:
        excluded.add(agent_source.source_id)
    context_text = dynamic_context_registry.provider_context_prompt(
        snapshot.source_inventory,
        exclude_source_ids=excluded,
    )
    context_block = (
        "--- BEGIN CORSIA CONTEXT SOURCES ---\n"
        f"{context_text}\n"
        "--- END CORSIA CONTEXT SOURCES ---\n\n"
        if context_text
        else ""
    )
    snapshot_ref = f"--- CORSIA CONTEXT SNAPSHOT: {snapshot.snapshot_id} ---\n\n"
    return f"{context_block}{snapshot_ref}User task:\n{user_text}", user_text


def build_codex_context_reconstruction(
    *,
    thread_id: str,
    user_text: str,
    user_source_text: str | None = None,
    agent_envelope=None,
    working_dir: str | None,
    project_id: str | None,
    model: str | None = None,
    model_context_window: int | None | object = _CONTEXT_UNSET,
    codex_thread_id: str | None = None,
    history_projection: PortableHistoryProjection,
    checkpoint_text: str | None = None,
    db=None,
    tool_result_budget_tokens: int | None = None,
    tool_argument_budget_tokens: int | None = None,
) -> CodexContextReconstruction:
    raw_user_text = user_text if user_source_text is None else user_source_text
    history_items = _checkpoint_history_items(checkpoint_text) + codex_inject_items(
        history_projection,
        tool_result_budget_tokens=tool_result_budget_tokens,
        tool_argument_budget_tokens=tool_argument_budget_tokens,
    )
    history_source = _portable_history_source(history_projection)
    (
        collection,
        harness_model_facts,
        sources,
        agent_source,
        resolved_working_dir,
    ) = _codex_context_sources(
        thread_id=thread_id,
        raw_user_text=raw_user_text,
        agent_envelope=agent_envelope,
        working_dir=working_dir,
        project_id=project_id,
        model=model,
        model_context_window=model_context_window,
        codex_thread_id=codex_thread_id,
        db=db,
    )
    _append_codex_history_sources(
        sources,
        thread_id=thread_id,
        checkpoint_text=checkpoint_text,
        history_items=history_items,
        history_source=history_source,
    )
    snapshot = _build_reconstruction_snapshot(
        thread_id=thread_id,
        project_id=project_id,
        collection=collection,
        harness_model_facts=harness_model_facts,
        resolved_working_dir=resolved_working_dir,
        sources=sources,
        history_items=history_items,
        raw_user_text=raw_user_text,
        user_text=user_text,
        history_source=history_source,
        checkpoint_text=checkpoint_text,
    )
    task_text, turn_text = _render_codex_context_text(
        snapshot,
        user_text=user_text,
        agent_source=agent_source,
    )
    return CodexContextReconstruction(
        task_text=task_text,
        turn_text=turn_text,
        snapshot=snapshot,
        history_format_version=history_projection.format_version,
        history_start_exclusive=history_projection.start_exclusive,
        history_sealed_boundary=history_projection.sealed_boundary,
        history_full_raw_replay_available=history_projection.full_raw_replay_available,
        history_items=history_items,
        checkpoint_text=checkpoint_text,
        installed=collection.installed,
    )
