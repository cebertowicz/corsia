"""Prepare the exact current-turn payload after Native prefix selection."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger

import core.native_context_prefix as native_prefix

if TYPE_CHECKING:
    from core.installed_context import InstalledDelivery
    from core.media_projection import ValidatedMediaProjection
    from infra.context_receipts import ContextReceiptIdentity


@dataclass(frozen=True)
class NativePreparedContext:
    """Exact Native payload frozen by send preflight."""

    messages: tuple[dict[str, Any], ...]
    context_usage: dict[str, Any]
    tool_definitions: tuple[dict[str, Any], ...]
    context_identity: ContextReceiptIdentity
    fit_ledger: Any | None = None
    # Private InstalledDelivery the dispatch retains; never published.
    installed: InstalledDelivery | None = field(
        default=None, compare=False, repr=False,
    )
    # Private snapshot this payload was rendered from (``core.guidance_manifest``).
    snapshot: Any | None = field(default=None, compare=False, repr=False)


def _content_only_tokens(content: str | list[dict[str, Any]] | None, *, model=None) -> int:
    """Estimate content bytes without the per-message framing tokens."""
    from core.token_estimation import _estimate_messages_tokens, _text_tokens

    if not content:
        return 0
    if isinstance(content, str):
        return _text_tokens(content, model)
    return max(
        0,
        _estimate_messages_tokens([{"role": "user", "content": content}], model=model) - 4,
    )


def _source_payload(agent, content, media, turn_model):
    if media:
        return agent.context._build_user_content(content or "", media, model=turn_model)
    return content


def _source_artifacts(prefix_info, messages, frozen_tools):
    from core.effective_context import provider_source_artifact

    return [
        *prefix_info["source_artifacts"],
        provider_source_artifact(
            harness="native",
            method="native-turn-messages",
            snapshot_id=prefix_info["snapshot_id"],
            payload={"messages": messages, "tools": frozen_tools},
        ),
    ]


def _context_usage(messages, source_payload, prefix_info, source_artifacts, *, model=None):
    from core.context_admission import fixed_prefix_section_tokens
    from core.destination_fit import partition_destination_sections
    from core.loaded_tool_usage import loaded_tool_tokens
    from core.token_estimation import _estimate_messages_tokens
    from tools.mcp_runtime import tool_server_index

    sections = prefix_info["source_section_tokens"]
    system_tokens = fixed_prefix_section_tokens(sections)
    tool_tokens = sum(int(sections.get(section) or 0) for section in ("tool", "tools"))
    history_tokens = prefix_info["history_tokens"] + sum(
        int(sections.get(section) or 0) for section in ("history", "summary")
    )
    model_context = prefix_info["model_context_window"]
    rendered_tokens = _estimate_messages_tokens(messages, model=model) + tool_tokens
    checkpoint_tokens = int(prefix_info.get("checkpoint_tokens") or 0)
    fit_tokens = partition_destination_sections(
        rendered_tokens,
        fixed=max(0, int(prefix_info.get("system_tokens") or 0) - checkpoint_tokens),
        checkpoint=checkpoint_tokens,
        tail=int(prefix_info.get("history_tokens") or 0),
        current_input=_estimate_messages_tokens([messages[-1]], model=model),
        tools=tool_tokens,
    )
    total_used = rendered_tokens
    usage = {
        "model_context_window": model_context,
        "system_tokens": system_tokens,
        "tool_def_tokens": tool_tokens,
        "history_tokens": history_tokens,
        "user_tokens": _content_only_tokens(source_payload, model=model),
        "context_source_tokens": sum(
            int(sections.get(section) or 0)
            for section in (
                "conductor", "project", "role", "thread", "dynamic", "provider",
            )
        ),
        "source_section_tokens": sections,
        "total_used": total_used,
        "rendered_input_tokens": rendered_tokens,
        "fit_section_tokens": fit_tokens,
        "utilization": round(total_used / model_context, 3) if model_context > 0 else 0,
        "was_trimmed": prefix_info["was_trimmed"],
        "history_messages": prefix_info["history_messages"],
        "loaded_tool_tokens": loaded_tool_tokens(messages, tool_server_index(), model=model),
        "snapshot_id": prefix_info["snapshot_id"],
        "context_snapshot": prefix_info["effective_context_snapshot"],
        "continuity_mode": prefix_info["continuity_mode"],
        **prefix_info["continuity_accounting"].receipt_fields(),
        "checkpoint_generation_id": prefix_info["checkpoint_generation_id"],
        "source_artifacts": source_artifacts,
    }
    from infra.lesson_usage_attribution import attach_lesson_estimates

    attach_lesson_estimates(usage)
    return usage


async def prepare_turn_context(
    agent,
    thread_id: str,
    user_content: str,
    turn_model: str,
    fresh_context: bool,
    project_id: str | None,
    channel: str,
    chat_id: str,
    media: list[str] | ValidatedMediaProjection | None,
    model_context_window: int | None | object = native_prefix.CONTEXT_WINDOW_UNSET,
    replay_selection=None,
    tool_definitions: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    user_source_content: str | None | object = native_prefix.USER_SOURCE_UNSET,
    agent_envelope=None,
    selected_model: str | None = None,
    installed_out: list[Any] | None = None,
    snapshot_out: list[Any] | None = None,
) -> tuple[list[dict], dict]:
    """Build LLM messages and context usage stats for a turn.

    ``installed_out`` receives the private installed delivery and
    ``snapshot_out`` the effective snapshot; neither enters the published usage.
    """
    from core.thread_history import prepare_history_for_llm
    import tools.confined_reads as confined_reads

    frozen_tools = native_prefix._selected_tool_definitions(
        await confined_reads.advertised(agent.tools, thread_id)
        if tool_definitions is None else tool_definitions
    )
    source_content = (
        user_content
        if user_source_content is native_prefix.USER_SOURCE_UNSET
        else user_source_content
    )
    source_payload = _source_payload(agent, source_content, media, turn_model)
    prefix, info = await native_prefix.build_real_send_prefix(
        agent=agent, thread_id=thread_id, turn_model=turn_model,
        fresh_context=fresh_context, project_id=project_id,
        current_user_content=user_content, model_context_window=model_context_window,
        replay_selection=replay_selection, tool_definitions=frozen_tools,
        current_user_source_content=source_payload,
        agent_envelope=agent_envelope,
        selected_model=selected_model,
    )
    current_payload = _source_payload(agent, user_content, media, turn_model)
    messages = [*prefix, {"role": "user", "content": current_payload}]
    if replay_selection is None:
        messages = prepare_history_for_llm(messages)
    logger.info("prepare_turn_context: built {} LLM messages (sanitized)", len(messages))
    if installed_out is not None:
        installed_out.append(info.get("installed_context"))
    if snapshot_out is not None:
        snapshot_out.append(info.get("effective_snapshot"))
    artifacts = _source_artifacts(info, messages, frozen_tools)
    usage = _context_usage(messages, source_payload, info, artifacts, model=turn_model)
    logger.info(
        "prepare_turn_context: system={} tools={} history={}({}msg) user={} "
        "rendered_system={} total={}/{} util={:.1%} trimmed={}",
        usage["system_tokens"], usage["tool_def_tokens"], usage["history_tokens"],
        info["history_messages"], usage["user_tokens"], info["system_tokens"],
        usage["total_used"], info["model_context_window"],
        usage["utilization"], info["was_trimmed"],
    )
    return messages, usage


async def prepare_native_context_payload(
    *,
    thread_id: str,
    user_content: str,
    target,
    project_id: str | None,
    media: list[str] | ValidatedMediaProjection | None,
    replay_selection,
    user_source_content: str | None | object = native_prefix.USER_SOURCE_UNSET,
    agent_envelope=None,
    runtime_context_epoch: int = 0,
) -> NativePreparedContext:
    """Render and freeze the exact Native payload used after admission."""
    from core.agent import get_agent
    from infra.context_receipts import ContextReceiptIdentity
    import tools.confined_reads as confined_reads

    agent = await get_agent()
    await agent._connect_mcp()
    frozen_tools = native_prefix._freeze_tool_definitions(
        await confined_reads.advertised(agent.tools, thread_id)
    )
    installed: list[Any] = []
    snapshot: list[Any] = []
    messages, usage = await prepare_turn_context(
        agent, thread_id, user_content, target.transport_model, False,
        project_id, "web", thread_id, media,
        model_context_window=target.context_window,
        replay_selection=replay_selection, tool_definitions=frozen_tools,
        user_source_content=user_source_content,
        agent_envelope=agent_envelope,
        selected_model=target.catalog_id,
        installed_out=installed,
        snapshot_out=snapshot,
    )
    return NativePreparedContext(
        messages=tuple(deepcopy(message) for message in messages),
        context_usage=deepcopy(usage),
        tool_definitions=tuple(deepcopy(tool) for tool in frozen_tools),
        context_identity=ContextReceiptIdentity(
            "native", target.catalog_id, runtime_context_epoch
        ),
        installed=installed[0] if installed else None,
        snapshot=snapshot[0] if snapshot else None,
    )
