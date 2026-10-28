"""Exact Native cached-prefix construction."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from loguru import logger

import core.native_context_sources as context_sources
import core.native_system_context as system_context
from infra.db_async import run_in_db


EXPECTED_USER_MSG_RESERVATION_TOKENS = 2000
CONTEXT_WINDOW_UNSET = object()
USER_SOURCE_UNSET = object()


def _freeze_tool_definitions(
    definitions: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    """Freeze the selected Native schemas once for proof, counting, and send."""
    return tuple(deepcopy(dict(definition)) for definition in definitions)


def _selected_tool_definitions(
    definitions: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    """Reuse an already-frozen selection; otherwise freeze it exactly once."""
    if isinstance(definitions, tuple):
        return definitions
    return _freeze_tool_definitions(definitions)


def _canonical_tool_definitions(definitions: tuple[dict[str, Any], ...]) -> str:
    """Return stable bytes for the exact schema ledger used by token estimates."""
    if not definitions:
        return ""
    return json.dumps(
        definitions, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )


def _load_history(
    *,
    thread_id: str,
    turn_model: str,
    fresh_context: bool,
    replay_selection,
) -> list[dict]:
    if fresh_context:
        logger.info(
            "build_real_send_prefix: fresh_context=True for session={} — "
            "skipping history load for isolated-context turn",
            thread_id,
        )
        return []
    if replay_selection is None:
        raise ValueError("Native continuation requires an explicit replay selection")
    from history.adapters import to_native_history
    from llm.capabilities import native_transport_identity
    from core.tool_result_projection import HARNESS_TOOL_RESULT_BUDGET_TOKENS

    provider, effective_model = native_transport_identity(turn_model)
    return to_native_history(
        replay_selection.tail,
        destination_provider=provider,
        destination_effective_model=effective_model,
        tool_result_budget_tokens=(
            HARNESS_TOOL_RESULT_BUDGET_TOKENS
            if replay_selection.mode == "checkpoint_tail"
            else None
        ),
        tool_argument_budget_tokens=(
            HARNESS_TOOL_RESULT_BUDGET_TOKENS
            if replay_selection.mode == "checkpoint_tail"
            else None
        ),
    )


def _resolved_context_window(turn_model: str, model_context_window) -> int:
    from llm.capabilities import get_model_context_window

    if model_context_window is CONTEXT_WINDOW_UNSET:
        return get_model_context_window(turn_model)
    return int(model_context_window or 0)


async def _source_material(
    *,
    agent,
    thread_id: str,
    project_id: str | None,
    replay_selection,
    selected_model: str | None,
):
    from infra.config import WORKSPACE

    workspace = getattr(agent.context, "workspace", WORKSPACE)
    collection = await run_in_db(
        context_sources._context_sources_for_prefix,
        workspace,
        thread_id,
        project_id,
        selected_model,
    )
    system_prompt, entries, handled = system_context._native_system_core(
        agent.context,
        project_id=project_id,
        collection=collection,
    )
    system_prompt, entries, handled = context_sources.append_thread_and_skills(
        system_prompt, entries, handled, collection, thread_id
    )
    from core.effective_context import ContextSourceEntry
    from eval.tool_allowlist import allowlist_hash, eval_tool_allowlist

    allowlist = eval_tool_allowlist()
    if allowlist is not None:
        canonical = json.dumps(list(allowlist), separators=(",", ":"))
        entries.append(ContextSourceEntry(
            source_id="eval-tool-allowlist",
            origin_section="evaluation",
            label="Disposable evaluation tool allowlist",
            stable_reference="env:CORSIA_EVAL_TOOL_ALLOWLIST",
            freshness="runtime",
            inclusion="included",
            reason="test-mode native tool restriction",
            owner_surface="tool-registry",
            prompt_content=canonical,
            metadata={"tool_names": list(allowlist), "sha256": allowlist_hash(allowlist)},
        ))
    checkpoint_prompt, checkpoint_component = context_sources.checkpoint_material(
        replay_selection
    )
    system_prompt += checkpoint_component
    return (
        collection, system_prompt, entries, handled, checkpoint_prompt,
        checkpoint_component,
    )


def _budgeted_prefix(
    *,
    history: list[dict],
    system_prompt: str,
    canonical_tools: str,
    replay_selection,
    model: str | None = None,
) -> tuple[list[dict], int, int, int]:
    from core.thread_history import prepare_history_for_llm
    from core.token_estimation import _estimate_messages_tokens, _text_tokens

    raw_history_tokens = _estimate_messages_tokens(history, model=model)
    system_tokens = _estimate_messages_tokens(
        [{"role": "system", "content": system_prompt}], model=model,
    )
    tool_tokens = _text_tokens(canonical_tools, model) + 4 if canonical_tools else 0
    prefix = [{"role": "system", "content": system_prompt}, *history]
    if replay_selection is None:
        prefix = prepare_history_for_llm(prefix)
    return prefix, system_tokens, tool_tokens, raw_history_tokens


def _effective_snapshot(
    *,
    thread_id: str,
    project_id: str | None,
    project_cwd: str,
    agent_facts,
    harness_model_facts: dict,
    source_entries,
    history,
    current_user_content,
    prefix,
    frozen_tools,
):
    from core.effective_context import build_effective_context_snapshot

    return build_effective_context_snapshot(
        thread_id=thread_id,
        reason="turn-prefix",
        scope_facts={
            "project_id": project_id,
            "scope_type": "project" if project_id else "workspace",
            "cwd": project_cwd,
            "source_root": project_cwd,
        },
        agent_facts=agent_facts,
        harness_model_facts=harness_model_facts,
        cwd=project_cwd,
        source_root=project_cwd,
        sources=source_entries,
        history=history,
        current_user_content=current_user_content,
        provider_payload={
            "native_prefix_messages": prefix,
            "tool_definitions": frozen_tools,
        },
        delivery_harnesses=("native",),
    )


def _prefix_info(
    *,
    model_context: int, system_tokens: int, checkpoint_component: str,
    tool_tokens: int,
    source_section_tokens: dict[str, int],
    history: list[dict], replay_selection, snapshot,
    prefix: list[dict], frozen_tools, installed=None,
) -> dict:
    from core.continuity_accounting import measure_delivered_block
    from core.effective_context import provider_source_artifact
    from core.token_estimation import _estimate_messages_tokens, _text_tokens

    model = snapshot.harness_model_facts.get("model")

    return {
        "model_context_window": model_context,
        "system_tokens": system_tokens,
        "checkpoint_tokens": _text_tokens(checkpoint_component, model),
        "tool_def_tokens": tool_tokens,
        "source_tokens": sum(source_section_tokens.values()),
        "source_section_tokens": source_section_tokens,
        "history_tokens": _estimate_messages_tokens(history, model=model),
        "msg_reservation_tokens": EXPECTED_USER_MSG_RESERVATION_TOKENS,
        "was_trimmed": False,
        "history_messages": len(history),
        "snapshot_id": snapshot.snapshot_id,
        "content_hash": snapshot.content_hash,
        "effective_context_snapshot": snapshot.to_dict(),
        "continuity_mode": replay_selection.mode if replay_selection else "full",
        "continuity_accounting": measure_delivered_block(checkpoint_component, model),
        "checkpoint_generation_id": (
            replay_selection.checkpoint_generation_id if replay_selection else None
        ),
        "source_artifacts": [
            provider_source_artifact(
                harness="native",
                method="native-prefix",
                snapshot_id=snapshot.snapshot_id,
                payload={"messages": prefix, "tools": frozen_tools},
            )
        ],
        "tool_definitions": frozen_tools,
        # Private InstalledDelivery; the dispatch retains it, never publishes it.
        "installed_context": installed,
        # Private snapshot the dispatch records its guidance list from.
        "effective_snapshot": snapshot,
    }


async def build_real_send_prefix(
    agent,
    thread_id: str,
    turn_model: str,
    fresh_context: bool,
    project_id: str | None,
    current_user_content: str | list[dict] | None = None,
    model_context_window: int | None | object = CONTEXT_WINDOW_UNSET,
    replay_selection=None,
    tool_definitions: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    current_user_source_content: str | list[dict[str, Any]] | None | object = USER_SOURCE_UNSET,
    agent_envelope=None,
    selected_model: str | None = None,
) -> tuple[list[dict], dict]:
    """Build the strict-prefix ``[system, ...history]`` message sequence."""
    from tools.catalog import advertised_tool_definitions

    if fresh_context and replay_selection is not None:
        raise ValueError("fresh Native context cannot consume a replay selection")
    history = _load_history(
        thread_id=thread_id, turn_model=turn_model,
        fresh_context=fresh_context, replay_selection=replay_selection,
    )
    model_context = _resolved_context_window(turn_model, model_context_window)
    material = await _source_material(
        agent=agent, thread_id=thread_id, project_id=project_id,
        replay_selection=replay_selection, selected_model=selected_model,
    )
    collection, system_prompt, entries, handled = material[:4]
    checkpoint_prompt, checkpoint_component = material[4:]
    frozen_tools = _selected_tool_definitions(
        advertised_tool_definitions(agent.tools)
        if tool_definitions is None else tool_definitions
    )
    canonical_tools = _canonical_tool_definitions(frozen_tools)
    prefix, system_tokens, tool_tokens, _raw_history_tokens = _budgeted_prefix(
        history=history, system_prompt=system_prompt,
        canonical_tools=canonical_tools,
        replay_selection=replay_selection,
        model=turn_model,
    )
    harness_facts = {
        "harness": "native", "model": turn_model,
        "model_context_window": model_context,
    }
    entries, section_tokens = context_sources.complete_source_inventory(
        source_entries=entries, collection=collection, handled_source_ids=handled,
        canonical_tool_definitions=canonical_tools,
        frozen_tool_definitions=frozen_tools, thread_id=thread_id,
        agent_envelope=agent_envelope, harness_model_facts=harness_facts,
        replay_selection=replay_selection, checkpoint_prompt=checkpoint_prompt,
        checkpoint_component=checkpoint_component, system_prompt=system_prompt,
        prefix=prefix,
    )
    snapshot = _effective_snapshot(
        thread_id=thread_id, project_id=project_id,
        project_cwd=collection.project_cwd, agent_facts=collection.agent_facts,
        harness_model_facts=harness_facts, source_entries=entries, history=history,
        current_user_content=(current_user_content if current_user_source_content
                              is USER_SOURCE_UNSET else current_user_source_content),
        prefix=prefix, frozen_tools=frozen_tools,
    )
    return prefix, _prefix_info(
        model_context=model_context, system_tokens=system_tokens,
        checkpoint_component=checkpoint_component,
        tool_tokens=tool_tokens,
        source_section_tokens=section_tokens,
        history=history, replay_selection=replay_selection, snapshot=snapshot,
        prefix=prefix, frozen_tools=frozen_tools, installed=collection.installed,
    )
