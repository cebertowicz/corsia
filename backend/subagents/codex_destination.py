"""Frozen Codex destination type and exact admission validation."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from core.token_estimation import _text_tokens
from subagents.codex_context_reconstruction import CodexContextReconstruction
from subagents.codex_runtime_errors import CodexPreparedContextStale
from subagents.codex_runtime_state import (
    _APP_SESSIONS,
    _CODEX_THREADS,
    _LIVE,
    CodexHandleFingerprint,
    current_handle_fingerprint,
)

if TYPE_CHECKING:
    from core.context_payload import PreparedContextPayload


@dataclass(frozen=True)
class PreparedCodexDestination:
    """Exact Codex task/turn/history bytes admitted by preflight."""

    thread_id: str
    target_catalog_id: str
    current_user_text: str
    delivery_mode: Literal["exact_replay", "checkpoint_tail", "provider_resume"]
    provider_thread_id: str | None
    handle_fingerprint: CodexHandleFingerprint | None
    reconstruction: CodexContextReconstruction
    provider_text: str
    history_items: tuple[dict[str, Any], ...]
    tool_registry: Any | None
    dynamic_tools: tuple[dict[str, Any], ...]
    input_tokens: int
    source_tokens: int
    source_section_tokens: tuple[tuple[str, int], ...]
    fit_section_tokens: tuple[tuple[str, int], ...]

    @property
    def accounting_payload(self) -> dict[str, Any]:
        """Return the exact model-facing task/turn/history representation."""
        cold = self.delivery_mode != "provider_resume"
        return {
            "task_text": self.provider_text if cold else None,
            "turn_text": None if cold else self.provider_text,
            "history_items": list(self.history_items),
            "dynamic_tools": list(self.dynamic_tools) if cold else [],
        }

    @property
    def source_section_token_map(self) -> dict[str, int]:
        return dict(self.source_section_tokens)


def _provider_projection(
    reconstruction: CodexContextReconstruction,
    delivery_mode: Literal["exact_replay", "checkpoint_tail", "provider_resume"],
    handle: CodexHandleFingerprint | None,
) -> tuple[str | None, str, tuple[dict[str, Any], ...]]:
    if delivery_mode == "provider_resume":
        if handle is None:
            raise ValueError("live Codex destination requires a handle fingerprint")
        return handle.codex_thread_id, reconstruction.turn_text, ()
    if handle is not None:
        raise ValueError("cold Codex destination cannot carry a live fingerprint")
    return None, reconstruction.task_text, reconstruction.history_items


def _source_section_tokens(reconstruction: CodexContextReconstruction, model) -> dict[str, int]:
    sections: dict[str, int] = {}
    for source in reconstruction.snapshot.source_inventory:
        if source.prompt_content:
            sections[source.origin_section] = (
                sections.get(source.origin_section, 0)
                + _text_tokens(source.prompt_content, model)
            )
    return sections


def _fit_sections(
    *,
    input_tokens: int,
    current_user_text: str,
    provider_text: str,
    history_items: tuple[dict[str, Any], ...],
    dynamic_tools: tuple[dict[str, Any], ...],
    cold: bool,
    checkpoint_present: bool,
    model: str | None,
):
    from core.destination_fit import partition_destination_sections

    checkpoint_items = history_items[:1] if checkpoint_present else ()
    tail_items = history_items[1:] if checkpoint_present else history_items
    current_tokens = _text_tokens(current_user_text, model)
    return partition_destination_sections(
        input_tokens,
        fixed=(
            max(0, _text_tokens(provider_text, model) - current_tokens) if cold else 0
        ),
        checkpoint=_text_tokens(json.dumps(checkpoint_items, default=str), model),
        tail=_text_tokens(json.dumps(tail_items, default=str), model),
        current_input=current_tokens,
        tools=_text_tokens(json.dumps(dynamic_tools, default=str), model),
    )


def build_prepared_codex_destination(
    *,
    thread_id: str,
    target_catalog_id: str,
    current_user_text: str,
    reconstruction: CodexContextReconstruction,
    delivery_mode: Literal["exact_replay", "checkpoint_tail", "provider_resume"],
    handle_fingerprint: CodexHandleFingerprint | None,
    tool_registry: Any | None,
    dynamic_tools: tuple[dict[str, Any], ...],
    model: str | None = None,
) -> PreparedCodexDestination:
    """Freeze the exact Codex payload preflight admits and runtime must send."""
    codex_thread_id, provider_text, history_items = _provider_projection(
        reconstruction, delivery_mode, handle_fingerprint
    )
    cold = delivery_mode != "provider_resume"
    accounting_payload = {
        "task_text": provider_text if cold else None,
        "turn_text": None if cold else provider_text,
        "history_items": list(history_items),
        "dynamic_tools": list(dynamic_tools) if cold else [],
    }
    input_tokens = _text_tokens(json.dumps(
        accounting_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ), model)
    checkpoint_present = any(
        source.source_id == "history:portable-checkpoint"
        for source in reconstruction.snapshot.source_inventory
    )
    fit_sections = _fit_sections(
        model=model,
        input_tokens=input_tokens,
        current_user_text=current_user_text,
        provider_text=provider_text,
        history_items=history_items,
        dynamic_tools=dynamic_tools,
        cold=cold,
        checkpoint_present=checkpoint_present,
    )
    sections = _source_section_tokens(reconstruction, model)
    return PreparedCodexDestination(
        thread_id=thread_id,
        target_catalog_id=target_catalog_id,
        current_user_text=current_user_text,
        delivery_mode=delivery_mode,
        provider_thread_id=codex_thread_id,
        handle_fingerprint=handle_fingerprint,
        reconstruction=reconstruction,
        provider_text=provider_text,
        history_items=history_items,
        tool_registry=tool_registry,
        dynamic_tools=dynamic_tools,
        input_tokens=input_tokens,
        source_tokens=sum(sections.values()),
        source_section_tokens=tuple(sorted(sections.items())),
        fit_section_tokens=tuple(fit_sections.items()),
    )


def validate_live_compaction(session: Any, target: Any) -> None:
    """Reject a session spawned under a compaction limit the target no longer has.

    The limit is fixed when the app-server starts, so the runtime cannot resume
    such a session; admitting it as ``provider_resume`` would fail after admission.
    """
    from subagents.codex_runtime_spawn import _compaction_overrides

    if session is not None and tuple(
        getattr(session, "config_overrides", ())
    ) != _compaction_overrides(
        target.context_window,
        discovered_context_window=target.discovered_context_window,
    ):
        raise CodexPreparedContextStale(
            "Codex live handle context window changed before context rendering"
        )


def _validate_destination_identity(
    destination: PreparedCodexDestination,
    *,
    thread_id: str,
    target_catalog_id: str | None,
    current_user_text: str,
    prepared_context: PreparedContextPayload,
) -> None:
    if (
        destination.thread_id != thread_id
        or destination.target_catalog_id != target_catalog_id
        or destination.current_user_text != current_user_text
    ):
        raise CodexPreparedContextStale(
            "Codex prepared destination does not match this thread/model/input"
        )
    handle = destination.handle_fingerprint
    if (
        handle is not None
        and handle.session_identity is not None
        and handle.runtime_context_epoch != prepared_context.runtime_context_epoch
    ):
        raise CodexPreparedContextStale(
            "Codex live handle belongs to an older runtime context epoch"
        )


def _live_tool_surface_changed(
    destination: PreparedCodexDestination,
    session,
) -> bool:
    handle = destination.handle_fingerprint
    if handle is None:
        return True
    actual_registry = getattr(session, "tool_registry", None)
    actual_tools = tuple(getattr(session, "dynamic_tools", ()) or ())
    if handle.session_identity is None:
        return session is not None or destination.tool_registry is None
    if destination.tool_registry is None:
        return actual_registry is not None or actual_tools != destination.dynamic_tools
    return (
        session is None
        or actual_registry is not destination.tool_registry
        or actual_tools != destination.dynamic_tools
    )


def _validate_live_destination(
    thread_id: str,
    destination: PreparedCodexDestination,
) -> None:
    if destination.handle_fingerprint != current_handle_fingerprint(thread_id):
        raise CodexPreparedContextStale(
            "Codex provider handle changed after context preflight"
        )
    live = _LIVE.get(thread_id)
    session = live.session if live is not None else _APP_SESSIONS.get(thread_id)
    if _live_tool_surface_changed(destination, session):
        raise CodexPreparedContextStale(
            "Codex provider tool surface changed after context preflight"
        )


def prepared_codex_destination(
    thread_id: str,
    *,
    target_catalog_id: str | None,
    current_user_text: str,
    prepared_context: PreparedContextPayload,
    allow_cold_with_handle: bool = False,
) -> PreparedCodexDestination:
    """Validate one frozen destination against the final runtime boundary."""
    if prepared_context.target_catalog_id != target_catalog_id:
        raise CodexPreparedContextStale(
            "Codex prepared context targets a different selected model"
        )
    destination = prepared_context.rendered.payload
    if not isinstance(destination, PreparedCodexDestination):
        raise CodexPreparedContextStale(
            "Codex preflight did not freeze an exact destination payload"
        )
    _validate_destination_identity(
        destination,
        thread_id=thread_id,
        target_catalog_id=target_catalog_id,
        current_user_text=current_user_text,
        prepared_context=prepared_context,
    )
    expected_mode = prepared_context.mode
    if destination.delivery_mode != expected_mode:
        raise CodexPreparedContextStale(
            "Codex prepared destination mode changed after preflight"
        )
    if destination.delivery_mode == "provider_resume":
        _validate_live_destination(thread_id, destination)
    elif not allow_cold_with_handle and (
        _LIVE.get(thread_id) is not None
        or _APP_SESSIONS.get(thread_id) is not None
        or _CODEX_THREADS.get(thread_id) is not None
    ):
        raise CodexPreparedContextStale(
            "Codex cold destination found an unretired provider handle"
        )
    return destination


async def resolve_prepared_codex_context(
    thread_id: str,
    *,
    target_catalog_id: str,
    current_user_text: str,
    prepared_context: PreparedContextPayload | None,
    refresh_prepared_context: Callable[[], Awaitable[PreparedContextPayload]] | None,
    allow_cold_with_handle: bool = False,
    refresh_admission_boundary: bool = False,
):
    """Validate admission under the transition lock against final live state."""
    if prepared_context is None:
        return None, None
    try:
        destination = prepared_codex_destination(
            thread_id,
            target_catalog_id=target_catalog_id,
            current_user_text=current_user_text,
            prepared_context=prepared_context,
            allow_cold_with_handle=allow_cold_with_handle,
        )
    except CodexPreparedContextStale:
        if refresh_prepared_context is None:
            raise
        prepared_context = await refresh_prepared_context()
        destination = prepared_codex_destination(
            thread_id,
            target_catalog_id=target_catalog_id,
            current_user_text=current_user_text,
            prepared_context=prepared_context,
            allow_cold_with_handle=allow_cold_with_handle,
        )
        return prepared_context, destination
    if refresh_admission_boundary:
        if refresh_prepared_context is None:
            raise CodexPreparedContextStale(
                "Codex send requires context re-preflight at its safe boundary"
            )
        refreshed = await refresh_prepared_context()
        if refreshed is prepared_context:
            raise CodexPreparedContextStale(
                "Codex send reused stale admission at its safe boundary"
            )
        prepared_context = refreshed
        destination = prepared_codex_destination(
            thread_id,
            target_catalog_id=target_catalog_id,
            current_user_text=current_user_text,
            prepared_context=prepared_context,
            allow_cold_with_handle=allow_cold_with_handle,
        )
    return prepared_context, destination


async def refresh_prepared_codex_context_after_boundary(
    thread_id: str,
    *,
    target_catalog_id: str,
    current_user_text: str,
    previous_context: PreparedContextPayload | None,
    refresh_prepared_context: Callable[[], Awaitable[PreparedContextPayload]] | None,
    allow_cold_with_handle: bool = False,
) -> PreparedContextPayload | None:
    """Require a new exact admission after interrupt/preempt changes history."""
    if previous_context is None:
        return None
    if refresh_prepared_context is None:
        raise CodexPreparedContextStale(
            "Codex provider boundary changed and requires context re-preflight"
        )
    refreshed = await refresh_prepared_context()
    if refreshed is previous_context:
        raise CodexPreparedContextStale(
            "Codex context refresh reused stale admission after boundary change"
        )
    prepared_codex_destination(
        thread_id,
        target_catalog_id=target_catalog_id,
        current_user_text=current_user_text,
        prepared_context=refreshed,
        allow_cold_with_handle=allow_cold_with_handle,
    )
    return refreshed
