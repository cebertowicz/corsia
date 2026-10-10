"""Collect and account the sources in one Codex reconstruction snapshot."""

from __future__ import annotations

from typing import Any

from core import dynamic_context_registry
from core.effective_context import (
    ContextSourceEntry,
    EffectiveContextSnapshot,
    build_effective_context_snapshot,
)
from core.model_context_sources import (
    agent_send_envelope_source_entry,
    model_context_source_entry,
)

CONTEXT_UNSET = object()


def _model_context_window(
    model: str | None,
    observed: int | None | object = CONTEXT_UNSET,
) -> int | None:
    if observed is not CONTEXT_UNSET:
        return int(observed) if observed else None
    if not model:
        return None
    from llm.capabilities import get_model_context_window

    return get_model_context_window(model)


def context_sources(
    *,
    thread_id: str,
    raw_user_text: str,
    agent_envelope,
    working_dir: str | None,
    project_id: str | None,
    model: str | None,
    model_context_window: int | None | object,
    codex_thread_id: str | None,
    db,
):
    user_task_source = ContextSourceEntry(
        source_id="codex-user-task",
        origin_section="user",
        label="Codex user task",
        stable_reference=f"thread://{thread_id}/current-user-task",
        freshness="fresh",
        inclusion="included",
        reason="Codex provider-bound task payload",
        owner_surface="chat-composer",
        prompt_content=raw_user_text,
        metadata={"thread_id": codex_thread_id},
    )
    harness_model_facts = {
        "harness": "codex",
        "model": model,
        "model_context_window": _model_context_window(
            model,
            model_context_window,
        ),
        "thread_id": codex_thread_id,
    }
    from infra.config import WORKSPACE

    collection = dynamic_context_registry.collect_context_sources(
        thread_id=thread_id,
        project_id=project_id,
        workspace=WORKSPACE,
        db=db,
        selected_model=model,
    )
    agent_source = agent_send_envelope_source_entry(
        thread_id=thread_id,
        harness="codex",
        agent_envelope=agent_envelope,
        model=model,
    )
    sources = [
        user_task_source,
        model_context_source_entry(harness_model_facts),
        *collection.sources,
    ]
    if agent_source is not None:
        sources.append(agent_source)
    return (
        collection,
        harness_model_facts,
        sources,
        agent_source,
        working_dir or collection.project_cwd,
    )


def append_history_sources(
    sources: list[ContextSourceEntry],
    *,
    thread_id: str,
    checkpoint_text: str | None,
    history_items: tuple[dict[str, Any], ...],
    history_source: dict[str, Any],
) -> None:
    if checkpoint_text:
        sources.append(
            ContextSourceEntry(
                source_id="history:portable-checkpoint",
                origin_section="history",
                label="Portable checkpoint",
                stable_reference=f"thread://{thread_id}/portable-checkpoint",
                freshness="fresh",
                inclusion="included",
                reason="provider-only checkpoint item before exact Codex tail",
                owner_surface="portable-checkpoint",
                metadata={"delivery_method": "thread/inject_items", "item_index": 0},
            )
        )
    if history_items:
        sources.append(
            ContextSourceEntry(
                source_id="codex-injected-history",
                origin_section="history",
                label="Codex injected CORSIA history",
                stable_reference=f"thread://{thread_id}/codex-history-items",
                freshness="fresh",
                inclusion="included",
                reason=(
                    "Codex thread/inject_items reconstruction from CORSIA DB history"
                ),
                owner_surface="codex-runtime",
                metadata={
                    "item_count": len(history_items),
                    "history_roles": [item.get("role") for item in history_items],
                    "portable_history": history_source,
                },
            )
        )


def build_reconstruction_snapshot(
    *,
    thread_id: str,
    project_id: str | None,
    collection,
    harness_model_facts: dict[str, Any],
    resolved_working_dir: str,
    sources: list[ContextSourceEntry],
    history_items: tuple[dict[str, Any], ...],
    raw_user_text: str,
    user_text: str,
    history_source: dict[str, Any],
    checkpoint_text: str | None,
) -> EffectiveContextSnapshot:
    return build_effective_context_snapshot(
        thread_id=thread_id,
        reason="codex-turn-payload",
        scope_facts={
            "project_id": project_id,
            "scope_type": "project" if project_id else "workspace",
        },
        agent_facts=collection.agent_facts,
        harness_model_facts=harness_model_facts,
        cwd=resolved_working_dir,
        source_root=resolved_working_dir,
        sources=sources,
        history=list(history_items),
        current_user_content=raw_user_text,
        provider_payload={
            "codex_user_text": user_text,
            "codex_injected_history_item_count": len(history_items),
            "portable_history": history_source,
            "checkpoint_text": checkpoint_text,
        },
        delivery_harnesses=("codex",),
    )
