"""Build the cache-stable Claude Code seed snapshot."""

from __future__ import annotations

from typing import Any

from core import dynamic_context_registry
from core.effective_context import ContextSourceEntry, build_effective_context_snapshot
from core.model_context_sources import model_context_source_entry
from subagents.cc_context_sources import append_replay_sources, collect_context_sources
from subagents.cc_context_types import CCContextReconstruction, CCTranscriptReconstruction


def _render_seed(snapshot, base_seed: str, *, omit_conductor: bool) -> str:
    inventory = (
        tuple(
            source for source in snapshot.source_inventory
            if source.metadata.get("provider") != "conductor_template"
        )
        if omit_conductor else snapshot.source_inventory
    )
    context_text = dynamic_context_registry.provider_context_prompt(
        inventory, exclude_source_ids={"cc-seed"}
    )
    context_block = (
        "--- BEGIN CORSIA CONTEXT SOURCES ---\n"
        f"{context_text}\n--- END CORSIA CONTEXT SOURCES ---\n\n"
        if context_text else ""
    )
    return (
        f"{base_seed.rstrip()}\n\n{context_block}"
        f"--- CORSIA CONTEXT SNAPSHOT: {snapshot.snapshot_id} ---\n"
    )


def _seed_inventory(
    thread_id: str,
    base_seed: str,
    *,
    project_id: str | None,
    is_subagent: bool,
    harness_model_facts: dict,
    db,
) -> tuple[Any, list]:
    from infra.config import WORKSPACE

    collection = collect_context_sources(
        thread_id=thread_id, project_id=project_id, workspace=WORKSPACE,
        db=db,
        selected_model=harness_model_facts.get("model"),
    )
    seed_source = ContextSourceEntry(
        source_id="cc-seed", origin_section="system",
        label="Claude Code seed file",
        stable_reference=f"thread://{thread_id}/cc-seed",
        freshness="fresh", inclusion="included",
        reason="Claude Code append-system-prompt-file delivery",
        owner_surface="cc-runtime", prompt_content=base_seed,
        metadata={"is_subagent": is_subagent},
    )
    return collection, [
        seed_source, model_context_source_entry(harness_model_facts),
        *collection.sources,
    ]


def build_cc_context_reconstruction(
    *,
    thread_id: str,
    base_seed: str,
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
) -> CCContextReconstruction:
    """Build the actual CC seed text from an effective-context snapshot."""
    if provider_handle_opaque and (history or reconstructed_transcript is not None):
        raise ValueError("provider-opaque Claude Code history cannot also be reconstructed")
    model_facts = {
        "harness": "claude-code", "model": catalog_model_id,
        "model_alias": model_alias,
    }
    collection, sources = _seed_inventory(
        thread_id, base_seed, project_id=project_id,
        is_subagent=is_subagent, harness_model_facts=model_facts, db=db,
    )
    provider_payload: dict[str, Any] = {
        "cc_seed_text": base_seed,
        "delivery_flag": "--append-system-prompt-file",
    }
    if provider_handle_opaque:
        provider_payload["history_source_attribution"] = "provider_handle_opaque"
    append_replay_sources(
        sources, provider_payload, thread_id=thread_id,
        checkpoint_text=checkpoint_text,
        reconstructed_transcript=reconstructed_transcript,
        checkpoint_reason="validated checkpoint plus exact Claude Code tail",
    )
    resolved_working_dir = working_dir or collection.project_cwd
    snapshot = build_effective_context_snapshot(
        thread_id=thread_id, reason="cc-seed",
        scope_facts={
            "project_id": project_id,
            "scope_type": "project" if project_id else "workspace",
        },
        agent_facts=collection.agent_facts, harness_model_facts=model_facts,
        cwd=resolved_working_dir, source_root=resolved_working_dir,
        sources=sources, history=history or [], provider_payload=provider_payload,
        delivery_harnesses=("claude-code",),
    )
    return CCContextReconstruction(
        seed_text=_render_seed(snapshot, base_seed, omit_conductor=False),
        persisted_seed_text=_render_seed(snapshot, base_seed, omit_conductor=True),
        snapshot=snapshot,
        installed=collection.installed,
    )
