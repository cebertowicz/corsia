"""Render and consume one exact preflight-frozen Claude Code destination."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from connections.account_runtime import thread_claude_dir
from core.tool_result_projection import HARNESS_TOOL_RESULT_BUDGET_TOKENS
from infra.database import get_db
from llm.resolution import ExecutionTarget
from subagents.cc_context_reconstruction import (
    CCPreparedDestination,
    CCPreparedTranscript,
    _transcript_path_for_cwd,
    build_cc_context_reconstruction,
    build_cc_user_frame_reconstruction,
    cc_snapshot_history,
    prepare_cc_transcript_from_corsia,
)
from subagents.cc_confinement import launch_confinement, launch_tools
from subagents.cc_invocation import LaunchConfinement
from subagents.cc_destination_consume import build_cc_user_frame as build_cc_user_frame
from subagents.cc_handle import live_handle_fingerprint, validate_prepared_live_handle
from subagents.cc_replay_authority import require_cc_replay_authority
from subagents.cc_runtime_errors import CCPreparedDestinationStale
from subagents.cc_runtime_helpers import (
    _artifact_cc_session_id,
    _build_main_seed,
    _build_seed,
    _main_working_dir,
    _proc_is_alive,
    native_stale_working_dir,
    registered_cwd_or_default,
)
from subagents.cc_runtime_state import _LIVE, _Live
from subagents.cc_sessions import cc_session_path


@dataclass(frozen=True, slots=True)
class _ColdMaterial:
    working_dir: str
    cc_session_id: str
    prior_cc_session_id: str | None
    history: tuple[dict, ...]
    transcript: CCPreparedTranscript | None
    checkpoint_text: str | None
    tool_registry: object
    tool_definitions: tuple | list
    confinement: LaunchConfinement


def prepared_destination_from_context(
    session_id: str,
    text: str,
    *,
    target: ExecutionTarget,
    prepared_context,
):
    """Return the exact destination already authorized by preflight."""
    return require_cc_replay_authority(
        session_id,
        text,
        target_catalog_id=target.catalog_id,
        prepared_context=prepared_context,
    ).destination


def _fit_sections(*, seed, transcript, frame, tool_definitions, checkpoint_text, model):
    from core.destination_fit import partition_destination_sections
    from core.token_estimation import _text_tokens

    tools_text = (
        json.dumps(
            tool_definitions, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, default=str,
        )
        if tool_definitions else ""
    )
    sections = {
        "seed": _text_tokens(seed.seed_text, model) if seed is not None else 0,
        "history": _text_tokens(transcript.content if transcript else "", model),
        "current_user_frame": _text_tokens(frame.turn_text, model),
        "tools": _text_tokens(tools_text, model) if tools_text else 0,
    }
    input_tokens = sum(sections.values())
    checkpoint_tokens = _text_tokens(checkpoint_text or "", model)
    fit = partition_destination_sections(
        input_tokens,
        fixed=max(0, sections["seed"] - checkpoint_tokens),
        checkpoint=checkpoint_tokens, tail=sections["history"],
        current_input=sections["current_user_frame"], tools=sections["tools"],
    )
    return sections, fit, input_tokens


def _provider_resume_tools(working_dir: str, live: _Live | None, confinement: LaunchConfinement):
    if live is not None and live.tool_registry is not None:
        return live.tool_registry, tuple(live.tool_definitions or ())
    return launch_tools(working_dir, confinement.confined)


def _require_session_in_root(live, provider_session_id: str, claude_dir: str) -> None:
    """Claude resumes only a transcript in its launch's root; a stranded one is rebuilt cold there."""
    if live is None and cc_session_path(provider_session_id, claude_dir=claude_dir) is None:
        raise CCPreparedDestinationStale(
            "Claude Code provider session is not in this thread's Claude root"
        )


def _provider_resume_destination(
    session_id: str,
    current_user_text: str,
    *,
    current_user_source_text: str | None,
    agent_envelope,
    target: ExecutionTarget,
    row,
    is_subagent: bool,
    claude_dir: str,
    db,
) -> CCPreparedDestination:
    default_working_dir = _main_working_dir(row["project_id"], db)
    live = _LIVE.get(session_id)
    if live is not None and (live.provider_context_untrusted
                             or not _proc_is_alive(live.session)):
        live = None
    if live is not None and native_stale_working_dir(
        getattr(live.session, "working_dir", None), default_working_dir,
    ) is not None:
        live = None
    fingerprint = None
    if live is not None:
        fingerprint = live_handle_fingerprint(session_id, live)
        validate_prepared_live_handle(
            session_id, live, target=target, fingerprint=fingerprint
        )
        if fingerprint.runtime_context_epoch != int(row["runtime_context_epoch"]):
            raise CCPreparedDestinationStale(
                "Claude Code provider session belongs to an older runtime context epoch"
            )
    registered_session_id = _artifact_cc_session_id(session_id, db)
    provider_session_id = (fingerprint.provider_session_id
                           if fingerprint is not None else None) or registered_session_id
    if not provider_session_id:
        raise CCPreparedDestinationStale("Claude Code provider session is not registered")
    _require_session_in_root(live, provider_session_id, claude_dir)
    confinement = launch_confinement(session_id, live, db)
    working_dir = ((getattr(live.session, "working_dir", None) if live is not None else None)
                   or registered_cwd_or_default(session_id, db, default_working_dir, confinement))
    base_seed = _build_seed(working_dir) if is_subagent else _build_main_seed(working_dir)
    seed = build_cc_context_reconstruction(
        thread_id=session_id,
        base_seed=base_seed,
        catalog_model_id=target.catalog_id,
        model_alias=target.transport_model,
        working_dir=working_dir,
        project_id=row["project_id"],
        is_subagent=is_subagent,
        history=[],
        provider_handle_opaque=True,
        db=db,
    )
    frame = build_cc_user_frame_reconstruction(
        thread_id=session_id, user_text=current_user_text,
        user_source_text=current_user_source_text, agent_envelope=agent_envelope,
        catalog_model_id=target.catalog_id, model_alias=target.transport_model,
        working_dir=working_dir, project_id=row["project_id"],
        is_subagent=is_subagent, history=[], provider_handle_opaque=True,
        db=db,
    )
    sections, fit, input_tokens = _fit_sections(
        model=target.expected_reported_model or target.transport_model,
        seed=None, transcript=None, frame=frame,
        tool_definitions=(), checkpoint_text=None,
    )
    registry, tool_definitions = _provider_resume_tools(working_dir, live, confinement)
    return CCPreparedDestination(
        thread_id=session_id, mode="provider_resume",
        target_catalog_id=target.catalog_id, current_user_text=current_user_text,
        working_dir=working_dir, cc_session_id=provider_session_id,
        prior_cc_session_id=registered_session_id,
        is_subagent=is_subagent, history=(), checkpoint_text=None,
        seed_reconstruction=seed, frame_reconstruction=frame, transcript=None,
        tool_registry=registry, tool_definitions=tool_definitions,
        live_handle_fingerprint=fingerprint, input_tokens=input_tokens,
        source_tokens=0, source_section_tokens=sections, fit_section_tokens=fit,
        confinement=confinement,
    )


def _cold_material(
    session_id: str,
    *,
    selection,
    target: ExecutionTarget,
    project_id: str | None,
    claude_dir: str,
    db,
) -> _ColdMaterial:
    from core.context_replay import checkpoint_prompt

    confinement = launch_confinement(session_id, None, db)
    working_dir = registered_cwd_or_default(
        session_id, db, _main_working_dir(project_id, db), confinement
    )
    prior_cc_session_id = _artifact_cc_session_id(session_id, db)
    cc_session_id = str(uuid.uuid4())
    tool_registry, tool_definitions = launch_tools(working_dir, confinement.confined)
    allowed_tools = frozenset(
        definition["function"]["name"] for definition in tool_definitions
    )
    adapter_args = {
        "cc_session_id": cc_session_id,
        "destination_provider": "anthropic",
        "destination_effective_model": target.expected_reported_model,
        "allowed_corsia_tool_names": allowed_tools,
        "tool_result_budget_tokens": HARNESS_TOOL_RESULT_BUDGET_TOKENS,
        "tool_argument_budget_tokens": (
            HARNESS_TOOL_RESULT_BUDGET_TOKENS
            if selection.mode == "checkpoint_tail"
            else None
        ),
    }
    history = tuple(cc_snapshot_history(selection.tail, **adapter_args))
    transcript = (
        prepare_cc_transcript_from_corsia(
            projection=selection.tail,
            path=_transcript_path_for_cwd(
                claude_dir=claude_dir, working_dir=working_dir,
                cc_session_id=cc_session_id,
            ),
            working_dir=working_dir,
            **adapter_args,
        )
        if history else None
    )
    checkpoint_text = checkpoint_prompt(selection) or None
    if selection.mode == "checkpoint_tail" and checkpoint_text is None:
        raise ValueError("Claude Code checkpoint-tail destination has no checkpoint")
    return _ColdMaterial(
        working_dir=working_dir,
        cc_session_id=cc_session_id,
        prior_cc_session_id=prior_cc_session_id,
        history=history,
        transcript=transcript,
        checkpoint_text=checkpoint_text,
        tool_registry=tool_registry,
        tool_definitions=tool_definitions,
        confinement=confinement,
    )


def _cold_destination(
    session_id: str,
    current_user_text: str,
    *,
    current_user_source_text: str | None,
    agent_envelope,
    selection,
    target: ExecutionTarget,
    project_id: str | None,
    is_subagent: bool,
    claude_dir: str,
    db,
) -> CCPreparedDestination:
    material = _cold_material(
        session_id, selection=selection, target=target,
        project_id=project_id, claude_dir=claude_dir, db=db,
    )
    transcript_reconstruction = (
        material.transcript.reconstruction if material.transcript else None
    )
    seed = build_cc_context_reconstruction(
        thread_id=session_id,
        base_seed=(
            _build_seed(material.working_dir)
            if is_subagent else _build_main_seed(material.working_dir)
        ),
        catalog_model_id=target.catalog_id, model_alias=target.transport_model,
        working_dir=material.working_dir, project_id=project_id,
        is_subagent=is_subagent, history=list(material.history),
        reconstructed_transcript=transcript_reconstruction,
        checkpoint_text=material.checkpoint_text, db=db,
    )
    frame = build_cc_user_frame_reconstruction(
        thread_id=session_id, user_text=current_user_text,
        user_source_text=current_user_source_text, agent_envelope=agent_envelope,
        catalog_model_id=target.catalog_id, model_alias=target.transport_model,
        working_dir=material.working_dir, project_id=project_id,
        is_subagent=is_subagent, history=list(material.history),
        reconstructed_transcript=transcript_reconstruction,
        checkpoint_text=material.checkpoint_text,
        db=db,
    )
    sections, fit, input_tokens = _fit_sections(
        model=target.expected_reported_model or target.transport_model,
        seed=seed, transcript=material.transcript, frame=frame,
        tool_definitions=material.tool_definitions,
        checkpoint_text=material.checkpoint_text,
    )
    return CCPreparedDestination(
        thread_id=session_id,
        mode="exact_replay" if selection.mode == "exact" else selection.mode,
        target_catalog_id=target.catalog_id, current_user_text=current_user_text,
        working_dir=material.working_dir, cc_session_id=material.cc_session_id,
        prior_cc_session_id=material.prior_cc_session_id,
        is_subagent=is_subagent, history=material.history,
        checkpoint_text=material.checkpoint_text,
        seed_reconstruction=seed, frame_reconstruction=frame,
        transcript=material.transcript, tool_registry=material.tool_registry,
        tool_definitions=material.tool_definitions,
        live_handle_fingerprint=None, input_tokens=input_tokens,
        source_tokens=sections["seed"] + sections["tools"],
        source_section_tokens=sections, fit_section_tokens=fit,
        confinement=material.confinement,
    )


def _validate_cold_selection(session_id: str, selection) -> None:
    if selection is None or selection.mode not in {
        "exact", "checkpoint_tail",
    }:
        raise ValueError("Claude Code cold destination requires exact replay")
    if (
        selection.tail.thread_id != session_id
        or selection.tail.history_epoch != selection.history_epoch
        or selection.tail.sealed_boundary != selection.sealed_boundary
    ):
        raise ValueError("Claude Code cold destination replay is inconsistent")


def prepare_cc_destination_payload(
    session_id: str,
    *,
    target: ExecutionTarget,
    current_user_text: str,
    current_user_source_text: str | None = None,
    agent_envelope=None,
    selection,
    provider_resume: bool,
    expected_selected_model: str | None = None,
    is_subagent: bool | None = None,
    db=None,
):
    """Render the exact CC seed/transcript/frame/tool payload for preflight."""
    if target.harness != "claude-code" or not target.transport_model:
        raise ValueError("CC destination render received a non-Claude Code target")
    owned_db = db or get_db()
    try:
        row = owned_db.execute(
            "SELECT project_id,spawned_by,model,runtime_context_epoch "
            "FROM threads WHERE id=?", (session_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Claude Code destination thread does not exist")
        selected = target.catalog_id if expected_selected_model is None else expected_selected_model
        if str(row["model"] or "").strip() != selected:
            raise ValueError("Claude Code destination source model changed before rendering")
        role = is_subagent if is_subagent is not None else row["spawned_by"] is not None
        claude_dir = thread_claude_dir(session_id, db=owned_db)
        if provider_resume:
            if selection is not None:
                raise ValueError("Claude Code live destination cannot include replay")
            return _provider_resume_destination(
                session_id, current_user_text,
                current_user_source_text=current_user_source_text,
                agent_envelope=agent_envelope, target=target,
                row=row, is_subagent=role, claude_dir=claude_dir, db=owned_db,
            )
        _validate_cold_selection(session_id, selection)
        return _cold_destination(
            session_id, current_user_text,
            current_user_source_text=current_user_source_text,
            agent_envelope=agent_envelope, selection=selection, target=target,
            project_id=row["project_id"], is_subagent=role,
            claude_dir=claude_dir, db=owned_db,
        )
    finally:
        if db is None:
            owned_db.close()


def provider_resume_seed(
    thread_id: str, target: ExecutionTarget,
) -> tuple[str, str]:
    """Rebuild the cache-bearing seed for one registered provider continuation."""
    destination = prepare_cc_destination_payload(
        thread_id, target=target, current_user_text="", selection=None,
        provider_resume=True, expected_selected_model=target.catalog_id,
    )
    return destination.cc_session_id, destination.seed_reconstruction.seed_text
