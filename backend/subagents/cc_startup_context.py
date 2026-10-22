"""Build the exact preflight-authorized context for one cold CC process."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.effective_context import provider_source_artifact
from core.installed_context import persist_installed_delivery
from llm.resolution import ExecutionTarget
from subagents.cc_confinement import (
    ConfinementRefused,
    registered_confinement,
    require_readable_dirs,
)
from subagents.cc_invocation import CONFINEMENT_NONE
from subagents.cc_replay_authority import require_cc_replay_authority
from subagents.cc_runtime_errors import CCPreparedDestinationStale
from subagents.cc_runtime_helpers import _artifact_cc_session_id, _write_seed_file


@dataclass(frozen=True, slots=True)
class CCStartupContext:
    """Frozen launch material consumed by the CC process owner."""

    row: Any
    is_subagent: bool
    model_alias: str
    target_context_window: int
    expected_reported_model: str | None
    working_dir: str
    cc_session_id: str
    prior_cc_session_id: str | None
    resume_session_id: str | None
    reconstructed_transcript: Any | None
    reconstruction: Any
    tool_registry: Any
    tool_definitions: tuple | list
    synthetic_resume: bool
    launch_identity: Any
    checkpoint_generation_id: str | None = None
    target_discovered_context_window: int | None = None
    confinement: str = CONFINEMENT_NONE
    add_dirs: tuple[str, ...] = ()


def _install_prepared_transcript(destination: Any) -> Any | None:
    if destination.transcript is None:
        if destination.history:
            raise ValueError(
                "Claude Code prepared history has no reconstructed transcript"
            )
        return None
    from subagents.cc_context_reconstruction import install_cc_prepared_transcript

    return install_cc_prepared_transcript(destination.transcript)


def _launch_confinement(
    session_id: str, working_dir: str, db: Any,
) -> tuple[str, tuple[str, ...]]:
    """Carry the registered confinement into this launch, rechecking its reads."""
    confinement, add_dirs, confined_dir = registered_confinement(session_id, db)
    if confinement == CONFINEMENT_NONE:
        return confinement, add_dirs
    if confined_dir != working_dir:
        raise ConfinementRefused(
            "confined Claude Code child lost its own working directory"
        )
    return confinement, require_readable_dirs(list(add_dirs), None)


def build_cc_startup_context(
    session_id: str,
    current_user_text: str,
    *,
    row: Any,
    target: ExecutionTarget,
    is_subagent: bool,
    prepared_context: Any,
    db: Any,
) -> CCStartupContext:
    """Bind one cold launch to the exact CORSIA replay prepared for it."""
    authority = require_cc_replay_authority(
        session_id,
        current_user_text,
        target_catalog_id=target.catalog_id,
        prepared_context=prepared_context,
    )
    destination = authority.destination
    if destination.is_subagent != is_subagent:
        raise ValueError("Claude Code prepared destination has the wrong thread role")
    registered_session_id = _artifact_cc_session_id(session_id, db)
    if destination.prior_cc_session_id != registered_session_id:
        raise CCPreparedDestinationStale(
            "Claude Code continuity registration changed after preflight"
        )
    transcript = (
        None
        if authority.source == "provider_resume"
        else _install_prepared_transcript(destination)
    )
    resume_session_id = (
        destination.cc_session_id
        if authority.source == "provider_resume"
        else (destination.cc_session_id if transcript is not None else None)
    )
    confinement, add_dirs = _launch_confinement(session_id, destination.working_dir, db)
    reconstruction = destination.seed_reconstruction
    if reconstruction is None:
        raise CCPreparedDestinationStale(
            "Claude Code process launch has no prepared seed"
        )
    replay = getattr(prepared_context, "replay", None)
    checkpoint_generation_id = (
        getattr(replay, "checkpoint_generation_id", None)
        if prepared_context.mode == "checkpoint_tail"
        else None
    )
    return CCStartupContext(
        row=row,
        is_subagent=is_subagent,
        model_alias=target.transport_model,
        target_context_window=target.context_window,
        target_discovered_context_window=target.discovered_context_window,
        expected_reported_model=target.expected_reported_model,
        working_dir=destination.working_dir,
        cc_session_id=destination.cc_session_id,
        prior_cc_session_id=destination.prior_cc_session_id,
        resume_session_id=resume_session_id,
        reconstructed_transcript=transcript,
        reconstruction=reconstruction,
        tool_registry=destination.tool_registry,
        tool_definitions=destination.tool_definitions,
        synthetic_resume=transcript is not None,
        launch_identity=destination.launch_identity,
        checkpoint_generation_id=checkpoint_generation_id,
        confinement=confinement,
        add_dirs=add_dirs,
    )


def record_cc_startup_context(
    session_id: str,
    context: CCStartupContext,
    *,
    db: Any,
) -> tuple[str, list[dict]]:
    """Persist the seed facts before the external process can accept input."""
    reconstruction = context.reconstruction
    seed_file = _write_seed_file(session_id, reconstruction.seed_text)
    artifacts = [
        provider_source_artifact(
            harness="claude-code",
            method="cc-seed",
            snapshot_id=reconstruction.snapshot_id,
            payload={"seed_file": seed_file, "seed_text": reconstruction.seed_text},
        )
    ]
    transcript = context.reconstructed_transcript
    if transcript is not None:
        artifacts.append(
            provider_source_artifact(
                harness="claude-code",
                method="cc-reconstructed-transcript",
                snapshot_id=reconstruction.snapshot_id,
                payload={
                    "path": transcript.path,
                    "cc_session_id": transcript.cc_session_id,
                    "content_hash": transcript.content_hash,
                    "line_count": transcript.line_count,
                },
            )
        )
    if context.resume_session_id is None and not context.is_subagent:
        from subagents.child_thread import record_seed

        record_seed(session_id, reconstruction.persisted_seed_text, db=db)
    # Every process launch reads the seed file, resumed or not.
    persist_installed_delivery(reconstruction.installed, mode="launch", db=db)
    return seed_file, artifacts
