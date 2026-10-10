"""Codex context-usage projection and provider observation.

Split from `codex_runtime_terminal.py`, which owns terminal publication; this
module owns turning a Codex usage payload into CORSIA context truth. It
mirrors the Claude Code side, where `cc_event_context_usage.py` already holds
the same concern separately from its terminal path.
"""

from __future__ import annotations

from core.continuity_accounting import with_continuity
from infra import provider_usage
from infra.context_receipts import (
    unavailable_context_usage,
)
from infra.context_usage_attribution import merge_context_usage_with_snapshot
from subagents.codex_runtime_state import (
    _LiveCodex,
    context_receipt_identity,
)


def provider_context_usage(
    usage: dict | None,
    *,
    selected_context_window: int | None = None,
    discovered_context_window: int | None = None,
) -> dict | None:
    """Project Codex final-call input occupancy into CORSIA context truth.

    ``selected_context_window`` is what CORSIA counts against and may be an
    operator cap below the provider's capacity. Codex owns its own
    conversation and compacts against the discovered window, so occupancy can
    exceed the cap; the discovered window travels with the row so the surface
    can name which limit it is showing (#1753).
    """
    from subagents.codex_app_server_events import exact_token_count
    from infra.context_receipts import positive_window
    from llm.context_window import effective_usage_window

    if not isinstance(usage, dict):
        return None
    input_tokens = exact_token_count(usage.get("input_tokens"))
    if input_tokens is None:
        return None
    cached_input_tokens = exact_token_count(usage.get("cached_input_tokens"))
    if cached_input_tokens is None:
        cached_input_tokens = 0
    reported_window = exact_token_count(usage.get("model_context_window")) or 0
    window = effective_usage_window(selected_context_window, reported_window) or 0
    utilization = input_tokens / window if window else None
    context_usage = {
        "model_context_window": window,
        "discovered_context_window": positive_window(
            discovered_context_window, absent=None,
        ),
        "system_tokens": 0,
        "tool_def_tokens": 0,
        "history_tokens": input_tokens,
        "user_tokens": 0,
        "total_used": input_tokens,
        "utilization": utilization,
        "provider_reported_input_tokens": input_tokens,
        # Diagnostic subset of input_tokens; never add it to occupancy.
        "provider_reported_cached_input_tokens": cached_input_tokens,
        "provider_reported_total_used": input_tokens,
        "provider_reported_utilization": (
            input_tokens / reported_window if reported_window else None
        ),
        "token_usage_available": True,
        "was_trimmed": False,
        "history_messages": 0,
    }
    if reported_window:
        context_usage["provider_reported_model_context_window"] = reported_window
    return context_usage


def live_measured_context_usage(
    thread_id: str,
    live: _LiveCodex,
) -> dict | None:
    """Return active measured usage, merged with its snapshot and continuity."""
    raw_usage = live.session.peek_active_usage()
    measured = provider_context_usage(
        raw_usage,
        selected_context_window=live.context_window,
        discovered_context_window=live.discovered_context_window,
    )
    identity = context_receipt_identity(thread_id, live)
    if measured is None or identity is None:
        return None
    return context_usage_with_snapshot(measured, live)


def context_usage_with_snapshot(context_usage: dict | None, live) -> dict | None:
    """Merge usage with the Turn's snapshot and continuity; live and terminal share it."""
    return with_continuity(merge_context_usage_with_snapshot(
        context_usage,
        snapshot=getattr(live, "context_snapshot", None),
        snapshot_id=getattr(live, "context_snapshot_id", None),
        source_artifacts=getattr(live, "source_artifacts", None),
        continuity_mode="reconstructed",
        known_tool_definitions=getattr(getattr(live, "session", None), "dynamic_tools", None),
    ), live.continuity_accounting)


def record_provider_context_observation(
    context_usage: dict | None,
    live,
    *,
    source: str,
) -> None:
    """Correct catalog context size from exact runtime truth when available."""
    if not isinstance(context_usage, dict):
        return
    window = context_usage.get("provider_reported_model_context_window")
    if (
        not isinstance(window, int)
        or window <= 0
        or not getattr(live, "model", None)
        or not getattr(live, "provider_reported_model", None)
    ):
        return
    from connections.harness_models import record_runtime_context_observation

    record_runtime_context_observation(
        "codex",
        selector=live.model,
        reported_identity=live.provider_reported_model,
        effective_tokens=window,
        source=source,
    )
