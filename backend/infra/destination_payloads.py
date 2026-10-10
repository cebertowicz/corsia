"""How each provider turns a replay selection into a payload to transmit.

One function per provider, each answering the same question in that provider's
own vocabulary: given this selection and this target, what exactly would be
sent, and what does it cost in tokens? Each returns a
``RenderedDestinationPayload`` so the shared assembly in
``infra.preflight_render`` never has to know which provider produced it.

Split out of a single 251-line dispatch in ``infra.send_preflight`` whose three
provider branches could only be told apart by reading all of it.
"""

from __future__ import annotations

from typing import Any

from infra.db_async import run_in_db
from infra.send_types import SendRejected
from llm.resolution import ExecutionTarget

class ExternalPreparedContextStale(RuntimeError):
    """A provider handle changed after runtime truth was sampled.

    Lives here because every raise site is in this module: the fit question is
    the only thing that samples runtime truth and then acts on it. It stayed in
    ``send_service`` when those raisers moved out, which left the spine owning
    an exception nothing there could raise.
    """


def _prepare_codex_preflight_destination(
    *,
    thread_id: str,
    target: ExecutionTarget,
    current_user_content: str,
    current_user_source_content: str,
    agent_envelope,
    selection,
    provider_resume: bool,
    history_epoch: int,
    sealed_boundary,
    expected_selected_model: str | None = None,
):
    """Render the exact Codex payload on the DB worker."""
    from subagents.codex_destination_render import (
        prepare_codex_destination_payload,
    )

    return prepare_codex_destination_payload(
        thread_id=thread_id,
        user_text=current_user_content,
        user_source_text=current_user_source_content,
        agent_envelope=agent_envelope,
        target=target,
        replay_selection=selection,
        provider_resume=provider_resume,
        history_epoch=history_epoch,
        sealed_boundary=sealed_boundary,
        expected_selected_model=expected_selected_model,
    )


def _native_rendered_payload(
    *,
    prepared_native,
    input_tokens,
    native_current_input,
    selection,
):
    """Shape a prepared native context into a rendered payload."""
    from core.context_payload import RenderedDestinationPayload

    rendered = RenderedDestinationPayload(
        payload=prepared_native,
        input_tokens=input_tokens,
        current_user_tokens=int(
            prepared_native.context_usage.get("user_tokens") or 0
        ),
        source_tokens=sum(
            int(value or 0)
            for value in (
                prepared_native.context_usage.get("source_section_tokens") or {}
            ).values()
        ),
        source_section_tokens=dict(
            prepared_native.context_usage.get("source_section_tokens") or {}
        ),
        accounting_payload={
            "messages": list(prepared_native.messages),
            "tools": list(prepared_native.tool_definitions),
        },
        fit_section_tokens=dict(
            prepared_native.context_usage.get("fit_section_tokens") or {}
        ),
        fit_current_input=native_current_input,
        fit_components={
            "fixed": list(prepared_native.messages[:-1]),
            "checkpoint": (
                selection.checkpoint_json if selection is not None else None
            ),
            "tail": selection.tail if selection is not None else None,
            "current_input": native_current_input,
            "tools": list(prepared_native.tool_definitions),
        },
    )
    return rendered


async def _render_native_destination(
    *,
    thread_id,
    thread_row,
    target,
    current_user_content,
    current_user_source_content,
    agent_envelope,
    attachments,
    media_projection,
    selection,
):
    """Render a native destination payload."""
    from core.turn_context import prepare_native_context_payload
    from ws.turn_helpers import build_agent_content

    agent_content, _legacy_media = build_agent_content(
        current_user_content,
        attachments or [],
    )
    prepared_native = await prepare_native_context_payload(
        thread_id=thread_id,
        user_content=agent_content,
        target=target,
        project_id=(
            thread_row["project_id"]
            if hasattr(thread_row, "keys") and "project_id" in thread_row.keys()
            else None
        ),
        media=(
            media_projection
            if media_projection is not None and media_projection.images
            else None
        ),
        replay_selection=selection,
        user_source_content=current_user_source_content,
        agent_envelope=agent_envelope,
        runtime_context_epoch=int(
            thread_row["runtime_context_epoch"]
            if hasattr(thread_row, "keys")
            and "runtime_context_epoch" in thread_row.keys()
            else 0
        ),
    )
    input_tokens = int(
        prepared_native.context_usage.get("rendered_input_tokens") or 0
    )
    native_current_input = (
        prepared_native.messages[-1] if prepared_native.messages else None
    )
    return _native_rendered_payload(
        prepared_native=prepared_native,
        input_tokens=input_tokens,
        native_current_input=native_current_input,
        selection=selection,
    )


async def _render_cc_destination(
    *,
    thread_id,
    target,
    current_user_content,
    current_user_source_content,
    agent_envelope,
    selection,
    provider_resume,
    expected_selected_model,
):
    """Render a Claude Code destination payload."""
    from core.context_payload import RenderedDestinationPayload

    from subagents import cc_runtime
    from subagents.cc_runtime_helpers import CCWorkingDirMissing

    try:
        destination = await run_in_db(
            cc_runtime.prepare_cc_destination_payload,
            thread_id,
            target=target,
            current_user_text=current_user_content,
            current_user_source_text=current_user_source_content,
            agent_envelope=agent_envelope,
            selection=selection,
            provider_resume=provider_resume,
            expected_selected_model=expected_selected_model,
        )
    except cc_runtime.CCPreparedDestinationStale as exc:
        raise ExternalPreparedContextStale(str(exc)) from exc
    except CCWorkingDirMissing as exc:
        raise SendRejected(
            422,
            f"Claude Code turn blocked: {exc}",
            metadata={
                "event_type": "harness.error",
                "harness": "claude-code",
                "cause": "project_working_directory_unresolved",
                "repair_owner": "ctx-stability",
            },
        ) from exc
    from core.token_estimation import _text_tokens

    rendered = RenderedDestinationPayload(
        payload=destination,
        input_tokens=destination.input_tokens,
        current_user_tokens=_text_tokens(current_user_source_content, target.expected_reported_model or target.transport_model),
        source_tokens=destination.source_tokens,
        source_section_tokens=destination.source_section_tokens,
        accounting_payload=destination.accounting_payload,
        fit_section_tokens=dict(destination.fit_section_tokens),
        fit_current_input=destination.frame_reconstruction.turn_text,
        fit_components={
            "fixed": (
                destination.seed_reconstruction.seed_text
                if destination.seed_reconstruction is not None
                else destination.live_handle_fingerprint
            ),
            "checkpoint": destination.checkpoint_text,
            "tail": (
                destination.transcript.content
                if destination.transcript is not None
                else None
            ),
            "current_input": destination.frame_reconstruction.turn_text,
            "tools": list(destination.tool_definitions),
        },
    )
    return rendered


async def _render_codex_destination(
    *,
    thread_id,
    target,
    current_user_content,
    current_user_source_content,
    agent_envelope,
    selection,
    provider_resume,
    live_history_epoch,
    live_sealed_boundary,
    expected_selected_model,
):
    """Render a codex destination payload."""
    from core.context_payload import RenderedDestinationPayload

    from subagents.codex_runtime_errors import CodexPreparedContextStale

    history_epoch = (
        selection.history_epoch
        if selection is not None
        else int(live_history_epoch or 0)
    )
    sealed_boundary = (
        selection.sealed_boundary if selection is not None else live_sealed_boundary
    )
    try:
        destination = await run_in_db(
            _prepare_codex_preflight_destination,
            thread_id=thread_id,
            target=target,
            current_user_content=current_user_content,
            current_user_source_content=current_user_source_content,
            agent_envelope=agent_envelope,
            selection=selection,
            provider_resume=provider_resume,
            history_epoch=history_epoch,
            sealed_boundary=sealed_boundary,
            expected_selected_model=expected_selected_model,
        )
    except CodexPreparedContextStale as exc:
        raise ExternalPreparedContextStale(str(exc)) from exc
    except ValueError as exc:
        if "Project working directory unresolved" not in str(exc):
            raise
        raise SendRejected(
            422,
            f"Codex turn blocked: {exc}",
            metadata={
                "event_type": "harness.error",
                "harness": "codex",
                "cause": "project_working_directory_unresolved",
                "repair_owner": "ctx-stability",
            },
        ) from exc
    from core.token_estimation import _text_tokens

    rendered = RenderedDestinationPayload(
        payload=destination,
        input_tokens=destination.input_tokens,
        current_user_tokens=_text_tokens(current_user_source_content, target.expected_reported_model or target.transport_model),
        source_tokens=destination.source_tokens,
        source_section_tokens=destination.source_section_token_map,
        accounting_payload=destination.accounting_payload,
        fit_section_tokens=dict(destination.fit_section_tokens),
        fit_current_input=destination.current_user_text,
        fit_components={
            "fixed": destination.provider_text,
            "checkpoint": (
                selection.checkpoint_json if selection is not None else None
            ),
            "tail": list(destination.history_items),
            "current_input": destination.current_user_text,
            "tools": list(destination.dynamic_tools),
        },
    )
    return rendered
