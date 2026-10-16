"""Exact Native system-prompt rendering and source accounting."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from core.effective_context import ContextSourceEntry


def _proof_only(entry: Any, *, reason: str) -> Any:
    """Keep a component inspectable when an exact aggregate owns its bytes."""
    if entry.inclusion not in {"included", "summarized"}:
        return entry
    return replace(entry, inclusion="proof", reason=reason)


def _incremental_prompt_tokens(contents: tuple[str, ...], *, model=None) -> tuple[int, ...]:
    """Partition the rendered total into nonnegative source shares plus framing."""
    from core.token_estimation import _text_tokens

    if not contents:
        return ()
    assembled = ""
    boundaries = [0]
    for content in contents:
        assembled += content
        boundaries.append(_text_tokens(assembled, model))
    # BPE can merge across a component boundary, reducing the prefix count.
    # Attribute those merged tokens to the earlier source without inventing
    # negative shares that inventory projection would later clamp and overcount.
    for index in range(len(boundaries) - 2, 0, -1):
        boundaries[index] = min(boundaries[index], boundaries[index + 1])
    deltas = [end - start for start, end in zip(boundaries, boundaries[1:])]
    deltas[0] += 4
    return tuple(deltas)


_BASE_ORIGINS = ("corsia", "system", "workspace", "project")
_WORKSPACE_SOURCE_ID = "native:workspace"
_NATIVE_SYSTEM_COMPONENT_IDS = frozenset({
    _WORKSPACE_SOURCE_ID,
    "native:thread-context",
    "native:skills-index",
    "history:portable-checkpoint",
})


def _retoken_native_system_components(
    entries: list[ContextSourceEntry],
    *,
    system_prompt: str,
    model: str | None = None,
) -> list[ContextSourceEntry]:
    """Charge one system-message estimate across its exact component rows."""
    indexes = [
        index
        for index, entry in enumerate(entries)
        if entry.source_id.startswith("native:system:")
        or entry.source_id in _NATIVE_SYSTEM_COMPONENT_IDS
    ]
    contents = tuple(entries[index].prompt_content or "" for index in indexes)
    if "".join(contents) != system_prompt:
        raise ValueError(
            "Native system component ledger does not reproduce rendered prompt"
        )
    allocations = _incremental_prompt_tokens(contents, model=model)
    allocated = dict(zip(indexes, allocations, strict=True))
    return [
        replace(entry, prompt_tokens_override=allocated[index])
        if index in allocated
        else entry
        for index, entry in enumerate(entries)
    ]


def _role_layers(collection: Any) -> tuple[tuple[Any, ...], ...]:
    sources = collection.sources
    return (
        (
            "agent-role",
            "role",
            "Agent role context",
            "corsia://context/roles/agent",
            "document-viewer",
            [
                entry
                for entry in sources
                if entry.metadata.get("provider") == "role_context"
                and entry.metadata.get("role") == "agent"
            ],
        ),
        (
            "conductor",
            "conductor",
            "Conductor and CORSIA agent templates",
            "corsia://context/conductor-templates",
            "conductor-designation",
            [entry for entry in sources if entry.origin_section == "conductor"],
        ),
        (
            "conductor-role",
            "role",
            "Conductor role context",
            "corsia://context/roles/conductor",
            "document-viewer",
            [
                entry
                for entry in sources
                if entry.metadata.get("provider") == "role_context"
                and entry.metadata.get("role") == "conductor"
            ],
        ),
        (
            "corsia-agent-role",
            "role",
            "CORSIA agent role context",
            "corsia://context/roles/corsia_agent",
            "document-viewer",
            [
                entry
                for entry in sources
                if entry.metadata.get("provider") == "role_context"
                and entry.metadata.get("role") == "corsia_agent"
            ],
        ),
        (
            "agent-owner-map",
            "role",
            "Agent responsibility map",
            "corsia://context/agent-owner-map",
            "agents",
            [
                entry
                for entry in sources
                if entry.metadata.get("provider") == "agent_owner_map"
            ],
        ),
    )


def _append_role_components(
    components: list[Any],
    role_layers: tuple[tuple[Any, ...], ...],
) -> dict[str, list[ContextSourceEntry]]:
    from core.context_builder import SystemPromptComponent
    from core.dynamic_context_registry import provider_context_prompt

    proofs: dict[str, list[ContextSourceEntry]] = {}
    for component_id, origin, label, reference, owner, entries in role_layers:
        delivered = [
            entry
            for entry in entries
            if entry.inclusion in {"included", "summarized"}
            and entry.prompt_content
        ]
        if delivered:
            proofs[component_id] = delivered
        prompt = provider_context_prompt(delivered)
        if prompt:
            components.append(
                SystemPromptComponent(
                    component_id=component_id,
                    origin_section=origin,
                    label=label,
                    stable_reference=reference,
                    owner_surface=owner,
                    content=f"\n\n---\n\n{prompt}",
                )
            )
    return proofs


def _component_rows(components: list[Any]) -> list[ContextSourceEntry]:
    return [
        ContextSourceEntry(
            source_id=(
                _WORKSPACE_SOURCE_ID
                if component.component_id == "workspace"
                else f"native:system:{component.component_id}"
            ),
            origin_section=component.origin_section,
            label=component.label,
            stable_reference=component.stable_reference,
            freshness="fresh",
            inclusion="included",
            reason="exact ContextBuilder component used for Native dispatch",
            owner_surface=component.owner_surface,
            prompt_content=component.content,
        )
        for component in components
    ]


def _ordered_rows(
    component_rows: list[ContextSourceEntry],
    base_proofs: list[ContextSourceEntry],
    role_layers: tuple[tuple[Any, ...], ...],
    role_proofs: dict[str, list[ContextSourceEntry]],
) -> list[ContextSourceEntry]:
    rows: list[ContextSourceEntry] = []
    for origin in _BASE_ORIGINS:
        rows.extend(row for row in component_rows if row.origin_section == origin)
        rows.extend(
            _proof_only(
                entry,
                reason="component proof; exact Native system aggregate owns bytes",
            )
            for entry in base_proofs
            if entry.origin_section == origin
        )
    for component_id, *_rest in role_layers:
        source_id = f"native:system:{component_id}"
        rows.extend(row for row in component_rows if row.source_id == source_id)
        rows.extend(
            _proof_only(
                entry,
                reason="component proof; exact Native role aggregate owns bytes",
            )
            for entry in role_proofs.get(component_id, [])
        )
    handled_component_ids = set(role_proofs)
    rows.extend(
        row
        for row in component_rows
        if row.origin_section not in _BASE_ORIGINS
        and row.source_id.removeprefix("native:system:")
        not in handled_component_ids
    )
    return rows


def _native_system_core(
    context: Any,
    *,
    project_id: str | None,
    collection: Any,
) -> tuple[str, list[ContextSourceEntry], set[str]]:
    """Consume one exact ContextBuilder render and project its component ledger.

    A real builder renders system and project files from the collected rows,
    so their bytes are the install boundary's on every turn, not disk's.
    """
    from core.context_builder import ContextBuilder, render_context_system_prompt

    render = (
        context.render_system_prompt(
            project_id=project_id, installed=list(collection.sources),
        )
        if isinstance(context, ContextBuilder)
        else render_context_system_prompt(context, project_id=project_id)
    )
    components = list(render.components)
    role_layers = _role_layers(collection)
    role_proofs = _append_role_components(components, role_layers)
    component_rows = _component_rows(components)
    base_proofs = [
        entry
        for entry in collection.sources
        if entry.origin_section in _BASE_ORIGINS
    ]
    rows = _ordered_rows(component_rows, base_proofs, role_layers, role_proofs)
    handled_entries = [
        *base_proofs,
        *(entry for entries in role_proofs.values() for entry in entries),
    ]
    return (
        "".join(component.content for component in components),
        rows,
        {entry.source_id for entry in handled_entries},
    )
