"""Reconcile a provider's reported usage against the snapshot's own accounting.

The provider tells us how many prompt tokens it billed. The context snapshot
records what CORSIA believes it sent, broken down by origin. This module is
where those two numbers are compared and the difference attributed, so the CTX
widget can say not just "how full" but "full of what".

Two rules here are deliberate whitelists rather than derivations, and both exist
so a future source cannot silently inflate an attribution total:

* ``_DELIVERED_INCLUSIONS`` — only ``included``/``summarized`` rows carry text
  the provider actually billed; ``preview``/``proof`` rows are projections.
* ``_DELIVERED_SECTION_BUCKETS`` — an ``origin_section`` counts only if it is
  listed. ``_NON_DELIVERED_SECTIONS`` records the rows that are real but must
  NOT be summed, each with the double-count it would otherwise cause.

This module only interprets live context measurements and holds no storage of
its own.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from core.token_estimation import _estimate_messages_tokens, _text_tokens

logger = logging.getLogger(__name__)


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, default=str))


# Inclusion states whose tokens are actually delivered into the provider prompt.
# This mirrors the delivered-material rule used everywhere else (``_source_text``
# / ``ContextSourceEntry.to_inventory_dict`` only emit prompt text + prompt_tokens
# for these two states). ``preview``/``proof`` rows are projections and payload
# proofs, never delivered text, so they must not count toward a delivered total.
_DELIVERED_INCLUSIONS = frozenset({"included", "summarized"})


def _included_prompt_tokens(source: dict[str, Any]) -> int:
    if source.get("inclusion") not in _DELIVERED_INCLUSIONS:
        return 0
    value = source.get("prompt_tokens")
    return int(value) if isinstance(value, (int, float)) and value > 0 else 0


def _content_tokens(value: Any, *, model: str | None = None) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return _text_tokens(value, model)
    if isinstance(value, list):
        return max(
            0,
            _estimate_messages_tokens([{"role": "user", "content": value}], model=model) - 4,
        )
    return _text_tokens(json.dumps(_jsonable(value), sort_keys=True), model)


# Canonical mapping of ``origin_section`` -> estimated-attribution bucket. This
# is a whitelist ON PURPOSE: source attribution must never be inflated by a
# future origin_section that nobody vetted. Adding a new injected source that
# should count means adding it here deliberately.
_DELIVERED_SECTION_BUCKETS: dict[str, str] = {
    "corsia": "system_tokens",
    "system": "system_tokens",
    "workspace": "system_tokens",
    "tool": "tool_def_tokens",
    "tools": "tool_def_tokens",
    "history": "history_tokens",
    "summary": "history_tokens",
    # Bootloader + runtime-injected sources are genuinely delivered payload.
    "project": "context_source_tokens",
    "role": "context_source_tokens",
    "thread": "context_source_tokens",
    "dynamic": "context_source_tokens",
    "conductor": "context_source_tokens",
    "provider": "context_source_tokens",
}


# Inventory rows that are real, but are NOT summed here as delivered payload:
#   user   -> counted once from frozen_prompt_material.current_user_content
#             (the "user" source row carries the same text -> would double-count)
#   model  -> a model-id fact row; carries no prompt payload
#   preflight -> a next-turn projection that already summarizes the whole turn
#                (system+history+user); counting it would double-count the turn
_NON_DELIVERED_SECTIONS = frozenset({"user", "model", "preflight"})


# Claude Code and Codex never report a "tool"/"tools" source (their own tool
# schemas never enter CORSIA's source_inventory), so their tool_def_tokens is
# always 0 without this. Whitelisted deliberately, same reasoning as the
# buckets above: an unreviewed harness value must not start claiming a
# tool-definition estimate.
_MCP_TOOL_DEFINITION_HARNESSES = frozenset({"claude-code", "codex"})


def _mcp_tool_definition_json(harness: str, definitions: tuple[dict[str, Any], ...]) -> str:
    """Serialize tool definitions exactly as the harness's own destination-fit
    code already does for the same tuple, so this display estimate can never
    diverge from what checkpoint fit/admission computed for the live session.

    Mirrors ``subagents.cc_destination._fit_sections`` (CC's "tools" section)
    and ``subagents.codex_destination`` (Codex's "tools" term) byte for byte;
    keep both in sync if either call site's serialization ever changes.
    """
    if not definitions:
        return ""
    if harness == "claude-code":
        return json.dumps(
            definitions, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, default=str,
        )
    return json.dumps(definitions, default=str)


def _mcp_tool_definition_tokens(
    model: str | None,
    harness: str,
    known_definitions: tuple[dict[str, Any], ...] | None,
) -> int | None:
    """Estimate the CORSIA MCP tool-definition size for the harness row.

    Returns ``None`` when no live definitions are known yet, distinct from a
    legitimately empty ledger (0), so the caller can say the estimate is
    unavailable instead of guessing from a rebuild against the global
    workspace (which could silently disagree with the tool set the harness
    session actually received).
    """
    if known_definitions is None:
        return None
    try:
        text = _mcp_tool_definition_json(harness, known_definitions)
    except Exception:
        logger.warning(
            "context usage: could not serialize the CORSIA MCP tool-definition "
            "ledger for the harness-row estimate", exc_info=True,
        )
        return None
    return _text_tokens(text, model) if text else 0


def _snapshot_usage_sections(snapshot: dict[str, Any]) -> dict[str, int]:
    sections = {
        "system_tokens": 0,
        "tool_def_tokens": 0,
        "history_tokens": 0,
        "user_tokens": 0,
        "context_source_tokens": 0,
    }
    for source in snapshot.get("source_inventory") or []:
        if not isinstance(source, dict):
            continue
        tokens = _included_prompt_tokens(source)
        if not tokens:
            continue
        section = source.get("origin_section")
        section_key = section if isinstance(section, str) else ""
        bucket = _DELIVERED_SECTION_BUCKETS.get(section_key)
        if bucket is not None:
            sections[bucket] += tokens
        elif section_key not in _NON_DELIVERED_SECTIONS:
            # Unknown origin_section carrying real tokens: refuse to silently
            # inflate estimated attribution. Skip it and surface it loudly so the
            # whitelist above gets updated deliberately.
            logger.warning(
                "context usage: origin_section %r is not in the delivered-total "
                "whitelist; %d tokens excluded from estimated attribution",
                section,
                tokens,
            )

    material = snapshot.get("frozen_prompt_material")
    model = (snapshot.get("harness_model_facts") or {}).get("model")
    if isinstance(material, dict):
        history = material.get("history")
        if isinstance(history, list):
            sections["history_tokens"] += _estimate_messages_tokens(history, model=model)
        sections["user_tokens"] += _content_tokens(material.get("current_user_content"), model=model)
    return sections


def _snapshot_attribution_total(sections: dict[str, int]) -> int:
    return sum(int(value or 0) for value in sections.values())


def _history_source_attribution(snapshot: dict[str, Any]) -> str | None:
    material = snapshot.get("frozen_prompt_material")
    if not isinstance(material, dict):
        return None
    provider_payload = material.get("provider_payload")
    if not isinstance(provider_payload, dict):
        return None
    attribution = provider_payload.get("history_source_attribution")
    return attribution if isinstance(attribution, str) and attribution else None


def _explicitly_stale_usage(incoming_usage: dict[str, Any] | None) -> bool:
    return bool(
        incoming_usage is not None
        and incoming_usage.get("token_usage_available") is False
        and isinstance(incoming_usage.get("stale_reason"), str)
        and incoming_usage.get("stale_reason")
    )


def _measured_total_from_usage(incoming_usage: dict[str, Any] | None) -> int | None:
    if (
        incoming_usage is None
        or incoming_usage.get("token_usage_available") is False
        or incoming_usage.get("token_source") == "estimated"
    ):
        return None
    for field in (
        "provider_reported_total_used",
        "effective_input_tokens",
        "total_used",
    ):
        candidate = incoming_usage.get(field)
        if isinstance(candidate, (int, float)) and candidate >= 0:
            return int(candidate)
    return None


def _apply_mcp_tool_definition_estimate(
    sections: dict[str, int],
    snapshot: dict[str, Any],
    harness: str,
    measured: bool,
    known_tool_definitions: tuple[dict[str, Any], ...] | None = None,
) -> bool:
    """Carve the CORSIA MCP tool-definition estimate into ``sections`` in place.

    Growing ``tool_def_tokens`` here shrinks ``attribution_delta_tokens`` below
    by the same amount, so the two rows never double-count. Returns whether a
    harness known to have tool definitions failed to produce an estimate, so
    the caller can say so instead of looking identical to 'none to report'.
    """
    if not (measured and sections["tool_def_tokens"] == 0 and harness in _MCP_TOOL_DEFINITION_HARNESSES):
        return False
    model = (snapshot.get("harness_model_facts") or {}).get("model")
    estimate = _mcp_tool_definition_tokens(model, harness, known_tool_definitions)
    if estimate is None:
        return True
    sections["tool_def_tokens"] = estimate
    return False


def _attribution_delta_reason(harness: str, mcp_tool_definition_estimate_failed: bool) -> str:
    if harness == "native":
        return "native framing/tokenizer variance"
    if mcp_tool_definition_estimate_failed:
        # Surface the failure on the one row the user actually sees, instead of
        # quietly folding it into the same reason a genuinely-absent ledger
        # would produce (see server logs for the underlying error).
        return "unobservable harness/provider overhead (CORSIA tool-definition estimate failed; see server logs)"
    return "unobservable harness/provider overhead"


def _resolve_model_context_window(snapshot: dict[str, Any], usage: dict[str, Any]) -> int:
    model_window = (snapshot.get("harness_model_facts") or {}).get("model_context_window")
    try:
        model_window_int = int(model_window or 0)
    except (TypeError, ValueError):
        model_window_int = 0
    try:
        provider_window_int = int(usage.get("provider_reported_model_context_window") or 0)
    except (TypeError, ValueError):
        provider_window_int = 0
    from llm.context_window import effective_usage_window

    return effective_usage_window(
        model_window_int or usage.get("model_context_window"), provider_window_int,
    ) or 0


def _attach_snapshot_projection(
    usage: dict[str, Any], snapshot: dict[str, Any],
    snapshot_id: str | None, continuity_mode: str,
) -> None:
    """Attach the selected snapshot without trial-only descriptor projections."""
    usage["snapshot_id"] = snapshot_id or snapshot.get("snapshot_id")
    usage["context_snapshot"] = snapshot
    usage["continuity_mode"] = continuity_mode


def _settle_measured_total(usage: dict[str, Any], sections: dict[str, int], measured_total: int,
                           harness: str, provider_history_opaque: bool, tool_estimate_failed: bool) -> None:
    """Keep the provider's total and attribute what the snapshot rows miss."""
    usage["total_used"] = measured_total
    remainder = measured_total - _snapshot_attribution_total(sections)
    if provider_history_opaque and remainder > 0 and not tool_estimate_failed:
        # A resumed session got only this turn's input; the rest is the conversation the
        # harness kept: History, by the preview's rule (context_preview_estimate._count_retained_once).
        usage["history_tokens"] = sections["history_tokens"] + remainder
        usage["attribution_delta_tokens"] = 0
        usage.pop("attribution_delta_reason", None)
        return
    usage["attribution_delta_tokens"] = remainder
    usage["attribution_delta_reason"] = _attribution_delta_reason(harness, tool_estimate_failed)


def merge_context_usage_with_snapshot(
    context_usage: dict[str, Any] | None,
    *,
    snapshot: dict[str, Any] | None,
    snapshot_id: str | None = None,
    source_artifacts: list[dict[str, Any]] | None = None,
    continuity_mode: str = "reconstructed",
    known_tool_definitions: tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any] | None:
    """Attach estimated source attribution without replacing measured usage."""
    if not isinstance(snapshot, dict):
        return context_usage

    incoming_usage = context_usage if isinstance(context_usage, dict) else None
    explicitly_stale = _explicitly_stale_usage(incoming_usage)
    measured_total = _measured_total_from_usage(incoming_usage)
    measured = measured_total is not None
    usage = dict(context_usage or {
        "model_context_window": 0,
        "system_tokens": 0,
        "tool_def_tokens": 0,
        "history_tokens": 0,
        "user_tokens": 0,
        "total_used": None,
        "utilization": None,
        "was_trimmed": False,
        "history_messages": 0,
    })
    provider_utilization = usage.get("provider_reported_utilization", usage.get("utilization"))
    if measured:
        usage["provider_reported_total_used"] = measured_total
    if measured and provider_utilization is not None:
        usage["provider_reported_utilization"] = provider_utilization

    sections = _snapshot_usage_sections(snapshot)
    harness = str((snapshot.get("harness_model_facts") or {}).get("harness") or "")
    mcp_tool_definition_estimate_failed = _apply_mcp_tool_definition_estimate(
        sections, snapshot, harness, measured, known_tool_definitions,
    )
    usage.update(sections)
    history_source_attribution = _history_source_attribution(snapshot)
    provider_history_opaque = history_source_attribution == "provider_handle_opaque"
    if history_source_attribution is not None:
        usage["history_source_attribution"] = history_source_attribution
    usage["token_usage_available"] = measured
    # Provenance of the delivered figures: "measured" when the harness actually
    # reported provider token usage, "estimated" when the row is reconstructed
    # from CORSIA's frozen snapshot alone (CC/Codex mid-turn). Never claim
    # measured for a reconstructed row.
    if measured:
        usage["token_source"] = "measured"
    elif explicitly_stale:
        usage.pop("token_source", None)
    elif provider_history_opaque:
        usage.pop("token_source", None)
    else:
        usage["token_source"] = "estimated"
    _attach_snapshot_projection(usage, snapshot, snapshot_id, continuity_mode)

    usage["model_context_window"] = _resolve_model_context_window(snapshot, usage)
    snapshot_total = _snapshot_attribution_total(sections)
    if measured_total is not None:
        _settle_measured_total(usage, sections, measured_total, harness,
                               provider_history_opaque, mcp_tool_definition_estimate_failed)
    elif explicitly_stale or provider_history_opaque:
        usage["total_used"] = None
        usage.pop("attribution_delta_tokens", None)
        usage.pop("attribution_delta_reason", None)
    elif snapshot_total > 0:
        usage["total_used"] = snapshot_total
        usage["attribution_delta_tokens"] = 0
        usage.pop("attribution_delta_reason", None)
    else:
        usage["total_used"] = None
        usage.pop("attribution_delta_reason", None)
    # Recompute utilization from the final total_used against the final
    # (possibly window-grown) context window. Doing it here — not only inside
    # the snapshot_total branch — means growing the window from
    # harness_model_facts can never leave a stale ratio when snapshot_total is 0
    # and we keep the incoming provider total.
    final_total = usage.get("total_used")
    usage_window = int(usage.get("model_context_window") or 0)
    if isinstance(final_total, (int, float)) and usage_window > 0:
        usage["utilization"] = round(int(final_total) / usage_window, 3)
    else:
        usage["utilization"] = None
    if source_artifacts:
        usage["source_artifacts"] = source_artifacts
    return _jsonable(usage)
