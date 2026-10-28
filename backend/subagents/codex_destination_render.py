"""Render the one exact Codex destination admitted by send preflight."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

from core.context_replay import checkpoint_prompt
from core.tool_result_projection import HARNESS_TOOL_RESULT_BUDGET_TOKENS
from history.types import PORTABLE_HISTORY_FORMAT_VERSION, PortableHistoryProjection
from infra.database import get_db
from llm.resolution import ExecutionTarget
from subagents import codex_confinement
from subagents.codex_confinement import tool_surface as _tool_surface
from subagents.codex_destination import (
    build_prepared_codex_destination,
    validate_live_compaction,
)

from subagents.codex_runtime_errors import CodexPreparedContextStale
from subagents.codex_runtime_state import (
    _APP_SESSIONS,
    _LIVE,
    CodexHandleFingerprint,
    current_handle_fingerprint,
)
from subagents.codex_runtime_support import working_dir_for_reconstruction


@dataclass(frozen=True)
class _DeliveryPreparation:
    fingerprint: CodexHandleFingerprint | None
    history_projection: PortableHistoryProjection
    checkpoint_text: str | None
    codex_thread_id: str | None
    delivery_mode: Literal["exact_replay", "provider_resume"]
    tool_registry: Any
    dynamic_tools: tuple[dict[str, Any], ...]


def codex_destination_scope(
    thread_id: str,
    *,
    target_catalog_id: str,
    expected_selected_model: str | None = None,
    db,
) -> tuple[str | None, str]:
    row = db.execute(
        "SELECT project_id,model,confinement FROM threads WHERE id=?", (thread_id,)
    ).fetchone()
    if row is None:
        raise CodexPreparedContextStale("Codex destination thread no longer exists")

    selected_model = target_catalog_id if expected_selected_model is None else expected_selected_model
    if str(row["model"] or "") != selected_model:
        raise CodexPreparedContextStale(
            "Codex destination model changed before context rendering"
        )
    project_id = row["project_id"]
    context = (
        codex_confinement.thread_context(thread_id, db=db)
        if row["confinement"] is not None else {}
    )
    working_dir = (
        codex_confinement.neutral_working_dir(thread_id)
        if context.get("confinement") is not None
        else working_dir_for_reconstruction(project_id, db=db)
    )
    if not working_dir:
        raise CodexPreparedContextStale(
            "Codex destination working directory is unresolved"
        )
    return project_id, working_dir


def _validate_live_model(
    fingerprint: CodexHandleFingerprint,
    target: ExecutionTarget,
) -> None:
    expected = target.expected_reported_model
    reported_model_changed = expected is not None and (
        fingerprint.expected_reported_model not in (None, expected)
        or fingerprint.provider_reported_model not in (None, expected)
    )
    if (
        fingerprint.session_identity is not None
        and (
            fingerprint.catalog_model_id != target.catalog_id
            or fingerprint.transport_model != target.transport_model
            or reported_model_changed
        )
    ):
        raise CodexPreparedContextStale(
            "Codex live handle model changed before context rendering"
        )


def _live_preparation(
    *,
    thread_id: str,
    target: ExecutionTarget,
    replay_selection,
    history_epoch: int,
    sealed_boundary,
    working_dir: str,
    thread_context=None,
) -> _DeliveryPreparation:
    if replay_selection is not None:
        raise ValueError("live Codex destination cannot include replay")
    fingerprint = current_handle_fingerprint(thread_id)
    if fingerprint is None:
        raise CodexPreparedContextStale(
            "Codex live handle vanished before context rendering"
        )
    _validate_live_model(fingerprint, target)
    if (
        fingerprint.working_dir is not None
        and fingerprint.working_dir != os.path.abspath(working_dir)
    ):
        raise CodexPreparedContextStale(
            "Codex live handle project changed before context rendering"
        )
    projection = PortableHistoryProjection(
        format_version=PORTABLE_HISTORY_FORMAT_VERSION,
        thread_id=thread_id,
        history_epoch=history_epoch,
        start_exclusive=sealed_boundary,
        sealed_boundary=sealed_boundary,
        turns=(),
        full_raw_replay_available=True,
    )
    live = _LIVE.get(thread_id)
    session = live.session if live is not None else _APP_SESSIONS.get(thread_id)
    validate_live_compaction(session, target)
    registry, tools = _tool_surface(working_dir, session, thread_context)
    return _DeliveryPreparation(
        fingerprint,
        projection,
        None,
        fingerprint.codex_thread_id,
        "provider_resume",
        registry,
        tools,
    )


def _cold_preparation(
    *,
    thread_id: str,
    replay_selection,
    history_epoch: int,
    sealed_boundary,
    working_dir: str,
    thread_context=None,
) -> _DeliveryPreparation:
    if replay_selection is None:
        raise ValueError("cold Codex destination requires replay")
    tail = replay_selection.tail
    if (
        tail.thread_id != thread_id
        or replay_selection.history_epoch != history_epoch
        or tail.history_epoch != history_epoch
        or replay_selection.sealed_boundary != sealed_boundary
        or tail.sealed_boundary != sealed_boundary
    ):
        raise ValueError("Codex replay does not match the admitted history boundary")
    registry, tools = _tool_surface(working_dir, None, thread_context)
    return _DeliveryPreparation(
        None,
        tail,
        checkpoint_prompt(replay_selection),
        None,
        (
            "exact_replay"
            if replay_selection.mode == "exact"
            else replay_selection.mode
        ),
        registry,
        tools,
    )


def _delivery_preparation(
    *,
    provider_resume: bool,
    thread_id: str,
    target: ExecutionTarget,
    replay_selection,
    history_epoch: int,
    sealed_boundary,
    working_dir: str,
    thread_context=None,
) -> _DeliveryPreparation:
    kwargs = {
        "thread_id": thread_id,
        "replay_selection": replay_selection,
        "history_epoch": history_epoch,
        "sealed_boundary": sealed_boundary,
        "working_dir": working_dir,
        "thread_context": thread_context,
    }
    if provider_resume:
        return _live_preparation(target=target, **kwargs)
    return _cold_preparation(**kwargs)


def _render_reconstruction(
    *,
    thread_id: str,
    user_text: str,
    user_source_text: str | None,
    agent_envelope,
    target: ExecutionTarget,
    project_id: str | None,
    working_dir: str,
    prepared: _DeliveryPreparation,
    db,
):
    from subagents.codex_context_reconstruction import (
        build_codex_context_reconstruction,
    )

    return build_codex_context_reconstruction(
        tool_result_budget_tokens=HARNESS_TOOL_RESULT_BUDGET_TOKENS,
        tool_argument_budget_tokens=(
            HARNESS_TOOL_RESULT_BUDGET_TOKENS
            if prepared.delivery_mode == "checkpoint_tail"
            else None
        ),
        thread_id=thread_id,
        user_text=user_text,
        user_source_text=user_source_text,
        agent_envelope=agent_envelope,
        working_dir=working_dir,
        project_id=project_id,
        model=target.catalog_id,
        model_context_window=target.context_window,
        codex_thread_id=prepared.codex_thread_id,
        history_projection=prepared.history_projection,
        checkpoint_text=prepared.checkpoint_text,
        db=db,
    )


def _require_unchanged_scope(
    *,
    thread_id: str,
    target: ExecutionTarget,
    expected_selected_model: str | None,
    project_id: str | None,
    working_dir: str,
    db,
) -> None:
    try:
        final_scope = codex_destination_scope(
            thread_id,
            target_catalog_id=target.catalog_id,
            expected_selected_model=expected_selected_model,
            db=db,
        )
    except CodexPreparedContextStale as exc:
        raise CodexPreparedContextStale(
            "Codex destination thread scope changed during context rendering"
        ) from exc
    final_project_id, final_working_dir = final_scope
    if (
        final_project_id != project_id
        or os.path.abspath(final_working_dir) != os.path.abspath(working_dir)
    ):
        raise CodexPreparedContextStale(
            "Codex destination thread scope changed during context rendering"
        )


def prepare_codex_destination_payload(
    *,
    thread_id: str,
    user_text: str,
    user_source_text: str | None = None,
    agent_envelope=None,
    target: ExecutionTarget,
    replay_selection,
    provider_resume: bool,
    history_epoch: int,
    sealed_boundary,
    expected_selected_model: str | None = None,
    db=None,
):
    """Freeze the exact Codex task/turn/history payload admitted by preflight."""
    if target.harness != "codex":
        raise ValueError("Codex destination render received a non-Codex target")
    connection = db or get_db()
    try:
        project_id, working_dir = codex_destination_scope(
            thread_id,
            target_catalog_id=target.catalog_id,
            expected_selected_model=expected_selected_model,
            db=connection,
        )
        prepared = _delivery_preparation(
            provider_resume=provider_resume,
            thread_id=thread_id,
            target=target,
            replay_selection=replay_selection,
            history_epoch=history_epoch,
            sealed_boundary=sealed_boundary,
            working_dir=working_dir,
            thread_context=codex_confinement.thread_context(thread_id, db=connection),
        )
        handle = prepared.fingerprint
        if handle is not None and handle.session_identity is not None:
            row = connection.execute(
                "SELECT runtime_context_epoch FROM threads WHERE id=?", (thread_id,),
            ).fetchone()
            if row is None or handle.runtime_context_epoch != row["runtime_context_epoch"]:
                raise CodexPreparedContextStale(
                    "Codex live handle belongs to an older runtime context epoch"
                )
        reconstruction = _render_reconstruction(
            thread_id=thread_id,
            user_text=user_text,
            user_source_text=user_source_text,
            agent_envelope=agent_envelope,
            target=target,
            project_id=project_id,
            working_dir=working_dir,
            prepared=prepared,
            db=connection,
        )
        _require_unchanged_scope(
            thread_id=thread_id,
            target=target,
            expected_selected_model=expected_selected_model,
            project_id=project_id,
            working_dir=working_dir,
            db=connection,
        )
        return build_prepared_codex_destination(
            model=target.expected_reported_model or target.transport_model,
            thread_id=thread_id,
            target_catalog_id=target.catalog_id,
            current_user_text=user_text,
            reconstruction=reconstruction,
            delivery_mode=prepared.delivery_mode,
            handle_fingerprint=prepared.fingerprint,
            tool_registry=prepared.tool_registry,
            dynamic_tools=prepared.dynamic_tools,
        )
    finally:
        if not db:
            connection.close()


def build_codex_reconstruction(
    *,
    thread_id: str,
    text: str,
    current_input_message_id: str | None,
    working_dir: str | None,
    project_id: str | None,
    model: str | None,
    context_window: int | None,
    codex_thread_id: str | None,
    db=None,
):
    """Build one cold or warm Codex frame from canonical portable history."""
    from history.projection import render_portable_history
    from subagents.codex_context_reconstruction import (
        build_codex_context_reconstruction,
    )

    connection = db or get_db()
    try:
        history_projection = render_portable_history(
            thread_id,
            db=connection,
            exclude_message_id=current_input_message_id,
        )
        return build_codex_context_reconstruction(
            thread_id=thread_id,
            user_text=text,
            working_dir=working_dir,
            project_id=project_id,
            model=model,
            model_context_window=context_window,
            codex_thread_id=codex_thread_id,
            history_projection=history_projection,
            db=connection,
        )
    finally:
        if not db:
            connection.close()
