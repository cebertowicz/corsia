"""Native context-source collection, rendering, and inventory ownership."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from core.dynamic_context_registry import ContextSourceCollection
from core.effective_context import ContextSourceEntry
from core.model_context_sources import agent_send_envelope_source_entry, model_context_source_entry
import core.native_system_context as system_context


def _message_text(messages: list[dict]) -> str:
    parts: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
    return "\n\n".join(parts)

def _reconcile_summary_sources_with_payload(
    source_entries: list[ContextSourceEntry], prefix: list[dict]
) -> list[ContextSourceEntry]:
    """Downgrade summary rows if budget trimming removed their prompt text."""
    payload_text = _message_text(prefix)
    reconciled: list[ContextSourceEntry] = []
    for entry in source_entries:
        if (
            isinstance(entry, ContextSourceEntry)
            and entry.origin_section == "summary"
            and entry.inclusion in {"included", "summarized"}
            and entry.prompt_content
            and entry.prompt_content not in payload_text
        ):
            entry = replace(
                entry,
                inclusion="omitted",
                reason="summary compacted out of provider payload",
                prompt_content=None,
                metadata={**entry.metadata, "compaction_status": "omitted_from_provider_payload"},
                prompt_tokens_override=None,
            )
        reconciled.append(entry)
    return reconciled


def _context_sources_for_prefix(
    workspace,
    thread_id: str,
    project_id: str | None,
    selected_model: str | None,
) -> ContextSourceCollection:
    from core.dynamic_context_registry import collect_context_sources
    from infra.database import get_db
    db = get_db()
    try:
        return collect_context_sources(
            thread_id=thread_id,
            project_id=project_id,
            workspace=workspace,
            db=db,
            selected_model=selected_model,
        )
    finally:
        db.close()


def _append_rendered_section(
    system_prompt: str,
    source_entries: list[ContextSourceEntry],
    handled_source_ids: set[str],
    entries: list[ContextSourceEntry],
    heading: str,
    aggregate: ContextSourceEntry,
) -> str:
    rendered = "\n\n".join(
        entry.prompt_content for entry in entries
        if entry.prompt_content and entry.inclusion in {"included", "summarized"}
    )
    if rendered:
        component = f"\n\n[{heading}]\n{rendered}"
        system_prompt += component
        source_entries.append(replace(aggregate, prompt_content=component))
        source_entries.extend(
            system_context._proof_only(
                entry,
                reason=f"component proof; exact Native {heading.partition(' ')[0].lower()} aggregate owns bytes",
            )
            for entry in entries
        )
    else:
        source_entries.extend(entries)
    handled_source_ids.update(entry.source_id for entry in entries)
    return system_prompt


def append_thread_and_skills(
    system_prompt: str,
    source_entries: list[ContextSourceEntry],
    handled_source_ids: set[str],
    collection: ContextSourceCollection,
    thread_id: str,
) -> tuple[str, list[ContextSourceEntry], set[str]]:
    """Append exact thread and skills aggregates in Native prefix order."""
    thread_entries = [entry for entry in collection.sources if entry.origin_section == "thread"]
    thread_aggregate = ContextSourceEntry(
        source_id="native:thread-context", origin_section="thread",
        label="Native thread context", stable_reference=f"thread://{thread_id}/thread-context",
        freshness="fresh", inclusion="included",
        reason="exact thread section rendered for Native dispatch", owner_surface="thread-settings",
    )
    system_prompt = _append_rendered_section(
        system_prompt, source_entries, handled_source_ids,
        thread_entries, "Thread context", thread_aggregate,
    )
    skill_entries = [
        entry for entry in collection.sources
        if (entry.metadata or {}).get("provider") == "skills"
    ]
    skills_aggregate = ContextSourceEntry(
        source_id="native:skills-index", origin_section="dynamic", label="Skills",
        stable_reference=f"thread://{thread_id}/skills/native-render",
        freshness="fresh", inclusion="included",
        reason="resolved skills index rendered for Native dispatch", owner_surface="skills",
        metadata={"provider": "skills"},
    )
    system_prompt = _append_rendered_section(
        system_prompt, source_entries, handled_source_ids,
        skill_entries, "Skills", skills_aggregate,
    )
    return system_prompt, source_entries, handled_source_ids


def checkpoint_material(replay_selection) -> tuple[str, str]:
    """Render the installed checkpoint and its exact system-prompt component."""
    if replay_selection is None:
        return "", ""
    from core.context_replay import checkpoint_prompt
    prompt = checkpoint_prompt(replay_selection)
    return prompt, f"\n\n{prompt}" if prompt else ""

def _append_inventory_front(
    source_entries: list[ContextSourceEntry], *, canonical_tool_definitions: str,
    frozen_tool_definitions: tuple[dict[str, Any], ...], thread_id: str,
    agent_envelope, harness_model_facts: dict[str, Any],
) -> None:
    from core.token_estimation import _text_tokens

    if canonical_tool_definitions:
        source_entries.append(ContextSourceEntry(
            source_id="native:tool-definitions", origin_section="tools",
            label="Native tool definitions", stable_reference=f"thread://{thread_id}/tool-definitions",
            freshness="fresh", inclusion="included", reason="exact selected Native tool schema ledger",
            owner_surface="tool-registry", prompt_content=canonical_tool_definitions,
            prompt_tokens_override=_text_tokens(
                canonical_tool_definitions, harness_model_facts.get("model"),
            ) + 4,
            metadata={"tool_count": len(frozen_tool_definitions)},
        ))
    envelope_source = agent_send_envelope_source_entry(
        thread_id=thread_id, harness="native", agent_envelope=agent_envelope,
        model=harness_model_facts.get("model"),
    )
    if envelope_source is not None:
        source_entries.append(envelope_source)
    source_entries.append(model_context_source_entry(harness_model_facts))


def _checkpoint_source(
    thread_id: str, replay_selection, checkpoint_component: str,
) -> ContextSourceEntry:
    boundary = replay_selection.checkpoint_boundary
    return ContextSourceEntry(
        source_id="history:portable-checkpoint", origin_section="history",
        label="Portable checkpoint",
        stable_reference=(f"thread://{thread_id}/portable-checkpoint/"
                          f"{replay_selection.checkpoint_generation_id or 'unknown'}"),
        freshness="fresh", inclusion="included",
        reason="validated checkpoint plus exact transcript tail",
        owner_surface="portable-checkpoint", prompt_content=checkpoint_component,
        metadata={
            "continuity_mode": replay_selection.mode,
            "checkpoint_boundary": boundary.seq if boundary is not None else None,
        },
    )


def _source_section_tokens(source_entries: list[ContextSourceEntry], *, model=None) -> dict[str, int]:
    totals: dict[str, int] = {}
    for entry in source_entries:
        tokens = entry.to_inventory_dict(model=model).get("prompt_tokens")
        if isinstance(tokens, int) and tokens > 0:
            section = str(entry.origin_section or "source")
            totals[section] = totals.get(section, 0) + tokens
    return totals

def complete_source_inventory(
    *, source_entries: list[ContextSourceEntry], collection: ContextSourceCollection,
    handled_source_ids: set[str], canonical_tool_definitions: str,
    frozen_tool_definitions: tuple[dict[str, Any], ...], thread_id: str,
    agent_envelope, harness_model_facts: dict[str, Any], replay_selection,
    checkpoint_prompt: str, checkpoint_component: str,
    system_prompt: str, prefix: list[dict],
) -> tuple[list[ContextSourceEntry], dict[str, int]]:
    """Complete, retoken, and reconcile the exact Native source inventory."""
    _append_inventory_front(
        source_entries, canonical_tool_definitions=canonical_tool_definitions,
        frozen_tool_definitions=frozen_tool_definitions, thread_id=thread_id,
        agent_envelope=agent_envelope, harness_model_facts=harness_model_facts,
    )
    source_entries.extend(
        entry for entry in collection.sources if entry.source_id not in handled_source_ids
    )
    if checkpoint_prompt:
        source_entries.append(_checkpoint_source(thread_id, replay_selection, checkpoint_component))
    source_entries = system_context._retoken_native_system_components(
        source_entries, system_prompt=system_prompt, model=harness_model_facts.get("model"),
    )
    source_entries = _reconcile_summary_sources_with_payload(source_entries, prefix)
    return source_entries, _source_section_tokens(
        source_entries, model=harness_model_facts.get("model"),
    )
