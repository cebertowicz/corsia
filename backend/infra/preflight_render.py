"""Assemble the context payload a fit decision is made against.

Dispatches to the provider that owns this thread's destination, then runs the
assembly every provider shares: stamp the fit identity, wrap the rendered
payload with the replay selection and target facts, and attach the fit ledger.

The public entry point is ``render_preflight_selection``. It has three callers
in three modules -- the fit decision in ``infra.send_preflight``, the checkpoint
owner, and the model-switch owner -- which is why it is a module of its own
rather than a private helper of any one of them.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import infra.destination_payloads as destination_payloads
from llm.resolution import ExecutionTarget

if TYPE_CHECKING:
    from core.media_projection import ValidatedMediaProjection


def _account_external_media(rendered, media_projection):
    """Add fresh image input to an external harness's frozen fit ledger."""
    from core.token_estimation import estimate_media_projection_tokens

    media_tokens = estimate_media_projection_tokens(media_projection)
    if not media_tokens:
        return rendered
    sections = dict(rendered.fit_section_tokens or {})
    sections["current_input"] = int(sections.get("current_input") or 0) + media_tokens
    return replace(
        rendered,
        input_tokens=int(rendered.input_tokens) + media_tokens,
        current_user_tokens=int(rendered.current_user_tokens) + media_tokens,
        fit_section_tokens=sections,
    )


async def _render_destination(
    provider: str,
    *,
    thread_id: str,
    thread_row: Any,
    target: ExecutionTarget,
    current_user_content: str,
    current_user_source_content: str,
    agent_envelope,
    attachments: list | None,
    media_projection,
    selection,
    live_handle: bool,
    live_history_epoch: int | None,
    live_sealed_boundary,
    expected_selected_model: str | None,
):
    if provider == "native":
        return await destination_payloads._render_native_destination(
            thread_id=thread_id, thread_row=thread_row, target=target,
            current_user_content=current_user_content,
            current_user_source_content=current_user_source_content,
            agent_envelope=agent_envelope,
            attachments=attachments, media_projection=media_projection,
            selection=selection,
        )
    if provider == "cc":
        return await destination_payloads._render_cc_destination(
            thread_id=thread_id, target=target,
            current_user_content=current_user_content,
            current_user_source_content=current_user_source_content,
            agent_envelope=agent_envelope, selection=selection,
            provider_resume=live_handle,
            expected_selected_model=expected_selected_model,
        )
    if provider == "codex":
        return await destination_payloads._render_codex_destination(
            thread_id=thread_id, target=target,
            current_user_content=current_user_content,
            current_user_source_content=current_user_source_content,
            agent_envelope=agent_envelope, selection=selection,
            provider_resume=live_handle, live_history_epoch=live_history_epoch,
            live_sealed_boundary=live_sealed_boundary,
            expected_selected_model=expected_selected_model,
        )
    raise ValueError(f"unsupported context preflight provider {provider!r}")


async def render_preflight_selection(
    *,
    thread_id: str,
    thread_row: Any,
    provider: str,
    target: ExecutionTarget,
    current_user_content: str,
    current_user_source_content: str,
    agent_envelope=None,
    attachments: list | None,
    media_projection: ValidatedMediaProjection | None = None,
    selection,
    live_handle: bool,
    live_history_epoch: int | None = None,
    live_sealed_boundary=None,
    expected_selected_model: str | None = None,
    input_identity: str,
    retained_provider_tokens: int = 0,
    canonical_input_tokens: int | None = None,
):
    """Render what would be sent, then assemble the fit-ready payload."""
    rendered = await _render_destination(
        provider,
        thread_id=thread_id, thread_row=thread_row, target=target,
        current_user_content=current_user_content,
        current_user_source_content=current_user_source_content,
        agent_envelope=agent_envelope,
        attachments=attachments, media_projection=media_projection,
        selection=selection, live_handle=live_handle,
        live_history_epoch=live_history_epoch,
        live_sealed_boundary=live_sealed_boundary,
        expected_selected_model=expected_selected_model,
    )
    if provider in {"cc", "codex"}:
        rendered = _account_external_media(rendered, media_projection)
    if live_handle and canonical_input_tokens is not None:
        retained_provider_tokens = max(
            retained_provider_tokens,
            canonical_input_tokens - int(rendered.input_tokens),
        )
    return _assemble_prepared_payload(
        rendered,
        thread_row=thread_row,
        target=target,
        selection=selection,
        provider_resume=live_handle,
        live_history_epoch=live_history_epoch,
        live_sealed_boundary=live_sealed_boundary,
        input_identity=input_identity,
        retained_provider_tokens=retained_provider_tokens,
    )


def _assemble_prepared_payload(
    rendered,
    *,
    thread_row,
    target,
    selection,
    provider_resume,
    live_history_epoch,
    live_sealed_boundary,
    input_identity,
    retained_provider_tokens,
):
    """Stamp fit identity onto a rendered payload and attach its ledger."""
    from core.context_payload import PreparedContextPayload
    from core.destination_preflight import attach_destination_fit_ledger

    rendered = replace(
        rendered,
        fit_input_identity=input_identity,
        fit_retained_provider_tokens=retained_provider_tokens,
    )
    payload_mode = getattr(rendered.payload, "mode", None)
    delivery_mode = getattr(rendered.payload, "delivery_mode", None)
    prepared = PreparedContextPayload(
        mode=(
            str(payload_mode or delivery_mode or "provider_resume")
            if provider_resume
            else (
                "exact_replay" if selection.mode == "exact" else selection.mode
            )
        ),
        target_catalog_id=target.catalog_id,
        model_context_window=target.context_window,
        history_epoch=(
            selection.history_epoch
            if selection is not None
            else int(live_history_epoch or 0)
        ),
        sealed_boundary=(
            selection.sealed_boundary if selection is not None else live_sealed_boundary
        ),
        replay=selection,
        rendered=rendered,
        runtime_context_epoch=int(
            thread_row["runtime_context_epoch"]
            if hasattr(thread_row, "keys") and "runtime_context_epoch" in thread_row.keys()
            else 0
        ),
    )
    return attach_destination_fit_ledger(
        prepared,
        harness=target.harness,
        model=target.expected_reported_model or target.transport_model,
        proven_target_window_tokens=target.context_window,
    )
