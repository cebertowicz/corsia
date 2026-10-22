"""Construct and register the single CC session selected during preflight."""

from __future__ import annotations

from typing import Any

from registry.cc_session import CCRegistrationChanged
from registry.service import register_cc_session
from subagents.cc_invocation import CONFINEMENT_NONE, permission_mode_for
from subagents.cc_runtime_errors import CCPreparedDestinationStale
from subagents.cc_runtime_state import _Live
from subagents.cc_startup_context import CCStartupContext


def build_starting_session(
    thread_id: str,
    live: _Live,
    context: CCStartupContext,
    *,
    binary: str,
    effort: str | None,
    on_event: Any,
    on_exit: Any,
    session_factory: Any,
):
    """Build the exact provider process frozen by destination preflight.

    A confined launch gets no CORSIA tool bridge; its read-only profile and
    add-dirs come from the launch record, so they survive every cold start.
    """
    from subagents.harness_tool_bridge import configure_cc_tool_bridge

    confined = context.confinement != CONFINEMENT_NONE
    mcp_config = None if confined else configure_cc_tool_bridge(
        live,
        thread_id=thread_id,
        working_dir=context.working_dir,
        registry=context.tool_registry,
    )
    transcript = context.reconstructed_transcript
    session = session_factory(
        binary=binary,
        session_id=context.cc_session_id,
        seed_file=live.seed_file,
        mcp_config=mcp_config,
        permission_mode=permission_mode_for(context.confinement),
        confinement=context.confinement,
        add_dirs=context.add_dirs,
        working_dir=context.working_dir,
        claude_dir=live.claude_dir,
        model=context.model_alias,
        effort=effort,
        on_event=on_event,
        on_exit=on_exit,
        resume_session_id=context.resume_session_id,
        corsia_thread_id=thread_id,
        reconstructed_transcript_path=(transcript.path if transcript else None),
    )
    session.corsia_turn_identity = live.corsia_turn
    return session


def install_starting_identity(
    thread_id: str, context: CCStartupContext, session: Any, db
) -> str | None:
    """Install the prepared identity only if its frozen predecessor is current."""
    try:
        return register_cc_session(
            thread_id,
            context.cc_session_id,
            title=context.row["title"],
            cwd=context.working_dir,
            invocation=session.invocation(),
            checkpoint_generation_id=context.checkpoint_generation_id,
            expected_previous_id=context.prior_cc_session_id,
            verify_previous=True,
            db=db,
        )
    except CCRegistrationChanged as error:
        raise CCPreparedDestinationStale(str(error)) from error


def restore_prior_identity(
    thread_id: str, context: CCStartupContext, previous_session_id: str | None, db
) -> None:
    """Undo one failed launch without overwriting a newer identity."""
    register_cc_session(
        thread_id,
        previous_session_id,
        title=context.row["title"],
        expected_previous_id=context.cc_session_id,
        verify_previous=True,
        db=db,
    )
