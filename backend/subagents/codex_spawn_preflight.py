"""Resolve and record one exact Codex app-server spawn payload."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.continuity_accounting import ContinuityAccounting, measure_delivered_block
from core.installed_context import persist_installed_delivery
from subagents.codex_context_reconstruction import CodexContextReconstruction
from subagents.codex_destination import prepared_codex_destination
from subagents.codex_destination_render import build_codex_reconstruction
from subagents.codex_runtime_config import codex_binary, codex_connection
from subagents.codex_runtime_errors import CodexPreparedContextStale
from subagents.codex_runtime_support import thread_runtime_context_epoch

if TYPE_CHECKING:
    from core.context_payload import PreparedContextPayload


@dataclass(frozen=True)
class CodexSpawnPayload:
    """Named provider bytes and context consumed by one app-server spawn."""

    reconstruction: CodexContextReconstruction
    provider_text: str
    history_items: tuple[dict[str, Any], ...]
    tool_registry: Any | None
    dynamic_tools: tuple[dict[str, Any], ...] | None
    continuity_accounting: ContinuityAccounting


def _delivered_continuity(
    checkpoint_text: str | None, *, resumed: bool, model: str | None,
) -> ContinuityAccounting:
    """The Codex delivery decision: what this spawn's payload actually carries.

    A resumed provider thread is sent no history, so its block cannot be
    observed; a cold spawn delivers the reconstruction's checkpoint text as its
    first history item, so that exact text is measured, never replayed messages.
    """
    if resumed:
        return ContinuityAccounting.provider_resume()
    return measure_delivered_block(checkpoint_text, model)


def validate_transport_model(model: str | None) -> str | None:
    """Validate the exact selector already resolved by ``ExecutionTarget``."""
    if not model:
        return None
    if "/" in model:
        raise ValueError(
            f"Codex transport requires an exact selector, got catalog id {model!r}"
        )
    return model


def resolve_spawn_payload(
    *, codex_thread_id: str | None, db, **kwargs: Any,
) -> CodexSpawnPayload:
    """Resolve one spawn payload and retain the sources a cold start delivers.

    Codex reads context sources only when it starts a thread; a resumed thread
    keeps the ones it started with, so only a cold spawn is retained.
    """
    payload = _spawn_payload(codex_thread_id=codex_thread_id, db=db, **kwargs)
    if codex_thread_id is None:
        persist_installed_delivery(
            getattr(payload.reconstruction, "installed", None), mode="launch",
            db=db,
        )
    return payload


def _spawn_payload(
    *,
    thread_id: str,
    text: str,
    current_input_message_id: str | None,
    codex_thread_id: str | None,
    catalog_model_id: str | None,
    context_window: int | None,
    prepared_context: PreparedContextPayload | None,
    working_dir: str | None,
    project_id: str | None,
    db,
):
    destination = None
    if prepared_context is not None:
        destination = prepared_codex_destination(
            thread_id,
            target_catalog_id=catalog_model_id,
            current_user_text=text,
            prepared_context=prepared_context,
        )
    if destination is not None:
        if destination.delivery_mode == "provider_resume" and (
            codex_thread_id != destination.provider_thread_id
        ):
            raise CodexPreparedContextStale(
                "Codex provider thread changed after context preflight"
            )
        if destination.delivery_mode != "provider_resume" and codex_thread_id is not None:
            raise CodexPreparedContextStale(
                "Codex cold reconstruction found an unprepared provider thread"
            )
        if destination.tool_registry is None:
            raise CodexPreparedContextStale(
                "Codex transport changed after exact destination preflight"
            )
        return CodexSpawnPayload(
            reconstruction=destination.reconstruction,
            provider_text=destination.provider_text,
            history_items=destination.history_items,
            tool_registry=destination.tool_registry,
            dynamic_tools=destination.dynamic_tools,
            continuity_accounting=_delivered_continuity(
                destination.reconstruction.checkpoint_text,
                resumed=destination.delivery_mode == "provider_resume",
                model=catalog_model_id,
            ),
        )
    reconstruction = build_codex_reconstruction(
        thread_id=thread_id,
        text=text,
        current_input_message_id=current_input_message_id,
        working_dir=working_dir,
        project_id=project_id,
        model=catalog_model_id,
        context_window=context_window,
        codex_thread_id=codex_thread_id,
        db=db,
    )
    provider_text = (
        getattr(reconstruction, "turn_text", text)
        if codex_thread_id
        else reconstruction.task_text
    )
    history_items = () if codex_thread_id else reconstruction.history_items
    return CodexSpawnPayload(
        reconstruction=reconstruction,
        provider_text=provider_text,
        history_items=history_items,
        tool_registry=None,
        dynamic_tools=None,
        continuity_accounting=_delivered_continuity(
            reconstruction.checkpoint_text, resumed=codex_thread_id is not None,
            model=catalog_model_id,
        ),
    )


def spawn_binary(binary: str | None, db) -> str:
    connection = codex_connection(db)
    return binary or codex_binary(connection)


def spawn_runtime_epoch(
    prepared_context: PreparedContextPayload | None,
    thread_id: str,
    db,
) -> int:
    if prepared_context is not None:
        return prepared_context.runtime_context_epoch
    return thread_runtime_context_epoch(thread_id, db=db)
