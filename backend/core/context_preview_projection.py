"""Sources, snapshot, and usage projection for the next-turn CTX preview.

Split out of context_preview.py (#1952) to keep both the file and
build_next_turn_context_preview under the repository's file-size ratchet.
The split follows the pipeline's own natural seam -- sources, then
snapshot, then usage -- rather than an arbitrary line cut, and
collect_context_sources stays in context_preview.py so its existing
monkeypatch test seam keeps working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.effective_context import (
    ContextSourceEntry,
    build_effective_context_snapshot,
    provider_source_artifact,
)
from core.model_context_sources import model_context_source_entry
from core.tool_definition_sources import tool_definition_entries
from core.pending_edit_marker import annotate_pending_edits
from infra.context_receipts import context_usage_for_boundary


def _preview_context_source(
    *,
    thread_id: str,
    provider: str,
    klass: str,
    is_subagent: bool,
    history_messages: int,
    preflight: dict[str, Any],
) -> ContextSourceEntry:
    return ContextSourceEntry(
        source_id="next-turn-preview",
        origin_section="preflight",
        label=f"{provider.title()} next-turn preview",
        stable_reference=f"thread://{thread_id}/next-turn-preview",
        freshness="preview",
        inclusion=(
            "included" if preflight["status"] == "allowed" else "omitted"
        ),
        reason=(
            "Exact frozen provider ledger projected before dispatch"
            if preflight["status"] in {"allowed", "blocked"}
            else "Provider payload is not frozen; dispatch admission is unknown"
        ),
        owner_surface="context-widget",
        metadata={
            "execution_class": klass,
            "provider": provider,
            "is_subagent": is_subagent,
            "history_message_count": history_messages,
            "source_artifacts_pending": True,
            "preflight": preflight,
        },
    )


def _preview_sources(
    *,
    thread_id: str,
    provider: str,
    source: ContextSourceEntry,
    harness_model_facts: dict[str, Any],
    collection: Any,
    canonical_tool_definitions: str | None,
    frozen_tool_definitions: Any,
    tool_def_tokens: int,
) -> list[ContextSourceEntry]:
    """Compose the already-resolved registry with the preview-only entries."""
    tool_sources = []
    if canonical_tool_definitions:
        tool_sources = tool_definition_entries(
            id_prefix=provider, thread_id=thread_id, freshness="preview",
            model=harness_model_facts.get("model"),
            canonical_tool_definitions=canonical_tool_definitions,
            frozen_tool_definitions=frozen_tool_definitions,
            total_tokens=tool_def_tokens, single_label="Tool definitions",
        )
    return [
        source,
        model_context_source_entry(harness_model_facts),
        *tool_sources,
        *collection.sources,
    ]


def _preview_snapshot(
    *,
    thread_id: str,
    project_id: str | None,
    collection: Any,
    harness_model_facts: dict[str, Any],
    sources: list[ContextSourceEntry],
    current_user_content: str,
    preflight: dict[str, Any],
    frozen_tool_definitions: Any,
    provider: str,
) -> Any:
    return build_effective_context_snapshot(
        thread_id=thread_id,
        reason="next-turn-preview",
        scope_facts={
            "project_id": project_id,
            "scope_type": "project" if project_id else "workspace",
        },
        agent_facts=collection.agent_facts,
        harness_model_facts=harness_model_facts,
        cwd=collection.project_cwd,
        source_root=collection.project_cwd,
        sources=sources,
        history=[],
        current_user_content=current_user_content,
        provider_payload={
            "preview": True,
            "preflight": preflight,
            "source_artifacts_pending": True,
            "tool_definitions": frozen_tool_definitions,
        },
        delivery_harnesses=(provider,),
        lifecycle_class="preview",
        retention_class="ephemeral",
    )


def _preview_usage(
    *,
    snapshot: Any,
    provider: str,
    preflight: dict[str, Any],
    frozen_tool_definitions: Any,
    discovered_context_window: int | None,
    system_tokens: int,
    tool_def_tokens: int,
    history_tokens: int,
    user_tokens: int,
    checkpoint_eligible: bool,
    checkpoint_ineligible_reason: str | None,
    history_messages: int,
) -> dict[str, Any]:
    return context_usage_for_boundary(
        snapshot=snapshot,
        source_artifacts=[
            provider_source_artifact(
                kind="provider-bound-payload-preview",
                harness=provider,
                method="next-turn-preview",
                snapshot_id=snapshot.snapshot_id,
                payload={
                    "preflight": preflight,
                    "source_artifacts_pending": True,
                    "actual_provider_artifacts": "generated at dispatch boundary",
                    "tool_definitions": frozen_tool_definitions,
                },
            )
        ],
        continuity_mode=preflight["continuity_mode"],
        extra={
            "preview": True,
            "token_usage_available": False,
            "token_source": "estimated",
            "source_artifacts_pending": True,
            "preflight": preflight,
            "model_context_window": preflight["model_context_window"],
            "discovered_context_window": discovered_context_window,
            "system_tokens": system_tokens,
            "tool_def_tokens": tool_def_tokens,
            "history_tokens": history_tokens,
            "user_tokens": user_tokens,
            "checkpoint_eligible": checkpoint_eligible,
            "checkpoint_ineligible_reason": checkpoint_ineligible_reason,
            "total_used": preflight["total_tokens"],
            "utilization": (
                round(
                    preflight["total_tokens"] / preflight["model_context_window"], 3
                )
                if preflight["model_context_window"] > 0
                else 0
            ),
            "was_trimmed": False,
            "history_messages": history_messages,
            "loaded_tool_tokens": preflight.get("loaded_tool_tokens") or [],
        },
    )


@dataclass(frozen=True, slots=True)
class PreviewInputs:
    """Every fact preview_usage_for_next_turn needs, bundled to keep its
    own signature from dominating the function-size ratchet."""

    thread_id: str
    provider: str
    model: str
    klass: str
    is_subagent: bool
    project_id: str | None
    collection: Any
    current_user_content: str
    preflight: dict[str, Any]
    frozen_tool_definitions: Any
    canonical_tool_definitions: str | None
    history_messages: int
    system_tokens: int
    tool_def_tokens: int
    history_tokens: int
    user_tokens: int
    checkpoint_eligible: bool
    checkpoint_ineligible_reason: str | None
    discovered_context_window: int | None


def preview_usage_for_next_turn(inputs: PreviewInputs) -> dict[str, Any]:
    """Compose the CTX widget usage payload for one previewed next turn."""
    harness_model_facts = {
        "harness": inputs.provider,
        "model": inputs.model,
        "execution_class": inputs.klass,
        "is_subagent": inputs.is_subagent,
    }
    source = _preview_context_source(
        thread_id=inputs.thread_id,
        provider=inputs.provider,
        klass=inputs.klass,
        is_subagent=inputs.is_subagent,
        history_messages=inputs.history_messages,
        preflight=inputs.preflight,
    )
    sources = _preview_sources(
        thread_id=inputs.thread_id,
        provider=inputs.provider,
        source=source,
        harness_model_facts=harness_model_facts,
        collection=inputs.collection,
        canonical_tool_definitions=inputs.canonical_tool_definitions,
        frozen_tool_definitions=inputs.frozen_tool_definitions,
        tool_def_tokens=inputs.tool_def_tokens,
    )
    snapshot = _preview_snapshot(
        thread_id=inputs.thread_id,
        project_id=inputs.project_id,
        collection=inputs.collection,
        harness_model_facts=harness_model_facts,
        sources=sources,
        current_user_content=inputs.current_user_content,
        preflight=inputs.preflight,
        frozen_tool_definitions=inputs.frozen_tool_definitions,
        provider=inputs.provider,
    )
    usage = _preview_usage(
        snapshot=snapshot,
        provider=inputs.provider,
        preflight=inputs.preflight,
        frozen_tool_definitions=inputs.frozen_tool_definitions,
        discovered_context_window=inputs.discovered_context_window,
        system_tokens=inputs.system_tokens,
        tool_def_tokens=inputs.tool_def_tokens,
        history_tokens=inputs.history_tokens,
        user_tokens=inputs.user_tokens,
        checkpoint_eligible=inputs.checkpoint_eligible,
        checkpoint_ineligible_reason=inputs.checkpoint_ineligible_reason,
        history_messages=inputs.history_messages,
    )
    return annotate_pending_edits(usage, inputs.collection)
