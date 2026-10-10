"""Build one provider-facing frame for a live Claude Code turn."""

from __future__ import annotations

from typing import Any

from core.continuity_accounting import ContinuityAccounting, measure_delivered_block
from core.effective_context import ContextSourceEntry, build_effective_context_snapshot
from core.model_context_sources import (
    agent_send_envelope_source_entry,
    model_context_source_entry,
)
from subagents.cc_context_sources import append_replay_sources, collect_context_sources
from subagents.cc_context_types import CCTranscriptReconstruction, CCUserFrameReconstruction


def _frame_inventory(
    thread_id: str,
    raw_user_text: str,
    *,
    project_id: str | None,
    is_subagent: bool,
    model_facts: dict,
    agent_envelope,
    db,
) -> tuple[Any, list]:
    from infra.config import WORKSPACE

    collection = collect_context_sources(
        thread_id=thread_id, project_id=project_id, workspace=WORKSPACE,
        db=db,
        selected_model=model_facts.get("model"),
    )
    frame_source = ContextSourceEntry(
        source_id="cc-user-frame", origin_section="user",
        label="Claude Code live user frame",
        stable_reference=f"thread://{thread_id}/cc-user-frame",
        freshness="fresh", inclusion="included",
        reason="Claude Code stream-json user-frame delivery",
        owner_surface="cc-runtime", prompt_content=raw_user_text,
        metadata={"is_subagent": is_subagent},
    )
    sources = [
        frame_source, model_context_source_entry(model_facts), *collection.sources,
    ]
    envelope_source = agent_send_envelope_source_entry(
        thread_id=thread_id, harness="claude-code", agent_envelope=agent_envelope,
        model=model_facts.get("model"),
    )
    if envelope_source is not None:
        sources.append(envelope_source)
    return collection, sources


def _frame_snapshot(
    *,
    thread_id: str,
    project_id: str | None,
    raw_user_text: str,
    collection,
    model_facts: dict,
    resolved_working_dir: str | None,
    sources: list,
    history: list[dict[str, Any]],
    provider_payload: dict[str, Any],
):
    return build_effective_context_snapshot(
        thread_id=thread_id, reason="cc-user-frame",
        scope_facts={
            "project_id": project_id,
            "scope_type": "project" if project_id else "workspace",
        },
        agent_facts=collection.agent_facts, harness_model_facts=model_facts,
        cwd=resolved_working_dir, source_root=resolved_working_dir,
        sources=sources, history=history, current_user_content=raw_user_text,
        provider_payload=provider_payload, delivery_harnesses=("claude-code",),
    )


def build_cc_user_frame_reconstruction(
    *,
    thread_id: str,
    user_text: str,
    user_source_text: str | None = None,
    agent_envelope=None,
    catalog_model_id: str | None,
    model_alias: str | None,
    working_dir: str | None,
    project_id: str | None,
    is_subagent: bool,
    history: list[dict[str, Any]] | None = None,
    reconstructed_transcript: CCTranscriptReconstruction | None = None,
    provider_handle_opaque: bool = False,
    checkpoint_text: str | None = None,
    db=None,
) -> CCUserFrameReconstruction:
    """Build the stream-json user-frame text sent to a live CC process."""
    if provider_handle_opaque and (history or reconstructed_transcript is not None):
        raise ValueError("provider-opaque Claude Code history cannot also be reconstructed")
    raw_user_text = user_text if user_source_text is None else user_source_text
    model_facts = {
        "harness": "claude-code", "model": catalog_model_id,
        "model_alias": model_alias,
    }
    collection, sources = _frame_inventory(
        thread_id, raw_user_text, project_id=project_id,
        is_subagent=is_subagent, model_facts=model_facts,
        agent_envelope=agent_envelope, db=db,
    )
    provider_payload: dict[str, Any] = {
        "cc_user_frame_text": user_text,
        "delivery_mode": "stream-json-user-frame",
    }
    if provider_handle_opaque:
        provider_payload["history_source_attribution"] = "provider_handle_opaque"
    append_replay_sources(
        sources, provider_payload, thread_id=thread_id,
        checkpoint_text=checkpoint_text,
        reconstructed_transcript=reconstructed_transcript,
        checkpoint_reason="validated checkpoint installed in Claude Code seed",
    )
    resolved_working_dir = working_dir or collection.project_cwd
    snapshot = _frame_snapshot(
        thread_id=thread_id, project_id=project_id,
        raw_user_text=raw_user_text,
        collection=collection, model_facts=model_facts,
        resolved_working_dir=resolved_working_dir, sources=sources,
        history=history or [], provider_payload=provider_payload,
    )
    frame_text = (
        f"--- CORSIA CONTEXT SNAPSHOT: {snapshot.snapshot_id} ---\n\n"
        f"User message:\n{user_text}"
    )
    # The CC delivery decision: a provider-opaque resume never carries the
    # checkpoint, so it must not report a block even if one was passed in.
    accounting = (
        ContinuityAccounting.provider_resume() if provider_handle_opaque
        else measure_delivered_block(checkpoint_text, catalog_model_id)
    )
    return CCUserFrameReconstruction(
        frame_text=frame_text, turn_text=user_text, snapshot=snapshot,
        continuity_accounting=accounting,
    )
