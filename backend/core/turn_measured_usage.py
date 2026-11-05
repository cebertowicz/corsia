"""Project a Turn's latest measured model call onto its context-usage record."""

from __future__ import annotations

from core.checkpoint_native_admission import effective_input_tokens
from infra.context_usage_attribution import merge_context_usage_with_snapshot


def context_usage_from_latest_call(
    preflight_usage: dict,
    last_call_usage: dict | None,
    *,
    include_context_details: bool = True,
) -> dict:
    """Project the latest measured model call onto the context record."""
    effective_input = effective_input_tokens(last_call_usage)
    measured = dict(preflight_usage)
    if not include_context_details:
        measured.pop("context_snapshot", None)
        measured.pop("source_artifacts", None)
        measured.pop("lesson_index_estimates", None)
    if effective_input is None:
        measured.update(
            total_used=None,
            utilization=None,
            token_usage_available=False,
            stale_reason="terminal_without_measurement",
        )
        measured.pop("token_source", None)
        return measured

    measured.update(
        effective_input_tokens=effective_input,
        total_used=effective_input,
        provider_reported_total_used=effective_input,
        token_usage_available=True, token_source="measured")
    measured.pop("stale_reason", None)
    window = measured.get("provider_reported_model_context_window") or measured.get(
        "model_context_window"
    )
    if isinstance(window, (int, float)) and window > 0:
        measured["provider_reported_utilization"] = round(effective_input / int(window), 3)
    for source, destination in (
        ("prompt_tokens", "provider_reported_input_tokens"),
        ("cache_read_tokens", "provider_reported_cache_read_input_tokens"),
        ("cache_creation_tokens", "provider_reported_cache_creation_input_tokens"),
    ):
        value = last_call_usage.get(source) if last_call_usage else None
        if isinstance(value, (int, float)) and value >= 0:
            measured[destination] = int(value)
    if not include_context_details:
        return measured
    return merge_context_usage_with_snapshot(
        measured, snapshot=measured.get("context_snapshot"),
        snapshot_id=measured.get("snapshot_id"), source_artifacts=measured.get("source_artifacts"),
        continuity_mode=measured.get("continuity_mode") or "full") or measured
