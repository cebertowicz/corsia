
from __future__ import annotations

from typing import Any, Callable

from .seam_latency import seam_latency_scorer
from .null_tool import null_tool_paired_scorer
from .structured_validity import structured_validity_scorer
from .registry import (
    check_event_present,
    check_event_metadata_relation,
    check_no_provider_error,
    check_assistant_message_content,
    check_history_contains,
    check_event_iteration_window,
    hit_rate_per_turn,
    system_prompt_stable,
    ttft_under,
    workflow_quality,
)


SCORER_REGISTRY: dict[str, Callable[..., Any]] = {
    "seam_latency": seam_latency_scorer,
    "null_tool_paired": null_tool_paired_scorer,
    "structured_validity": structured_validity_scorer,
    "check_event_present": check_event_present,
    "check_event_metadata_relation": check_event_metadata_relation,
    "check_no_provider_error": check_no_provider_error,
    "check_assistant_message_content": check_assistant_message_content,
    "check_history_contains": check_history_contains,
    "check_event_iteration_window": check_event_iteration_window,
    "hit_rate_per_turn": hit_rate_per_turn,
    "system_prompt_stable": system_prompt_stable,
    "ttft_under": ttft_under,
    "workflow_quality": workflow_quality,
    "check_event_suppressed": None,
    "check_drain_latency_ms": None,
    "check_no_orphan_tool_messages": None,
    "check_iteration_count_le": None,
    "check_compaction_event_fired": None,
    "check_subprocess_called_with_args": None,
    "check_subprocess_NOT_called_with_args": None,
    "check_tool_call_refused": None,          # E13 — tool-gate refusal event
    "check_no_subprocess": None,              # E13 — sandbox subprocess negative assertion
    "check_workspace_files_unchanged": None,  # E13 — filesystem-gate negative assertion
    "check_event_absent": None,               # E13 — generic event-absence assertion
    "check_prefill_floor_recorded": None,         # C01, C05 — record (no pass/fail) prefill floor for a model
    "check_per_turn_hit_rate_above": None,        # C02-C08, C10, C11 — cached_tokens/prompt_tokens ≥ threshold over a turn range
    "check_ttft_drop_after_turn_1": None,         # C02, C03 — TTFT(turn N>1) ≤ factor × TTFT(turn 1)
    "check_system_prompt_sha256_stable": None,    # C03 — system-prompt content hash unchanged across turn range
    "check_prefill_new_tokens_bounded": None,     # C03 — new-prefill-tokens per turn ≤ delta_tokens + slack
    "check_ssd_restore_observed": None,           # C07 — SSD-tier cache restore event observed after eviction
    "check_ttft_inflation_recorded": None,        # C09 — record (no pass/fail) TTFT inflation under contention
    "check_ttft_inflation_below_threshold": None, # C09, C11 — TTFT inflation under contention ≤ threshold
}


MODE_DEFAULTS: dict[str, list[str]] = {
    "ttft_burst": ["null_tool_paired"],
    "throughput_sustain": ["null_tool_paired"],
    "latency_tool_loops": ["seam_latency", "null_tool_paired"],
    "output_stability": ["structured_validity", "null_tool_paired"],
    "functional_behavior": [],
}


def _resolve_one(scorer_decl: Any) -> tuple[str, dict] | None:
    if isinstance(scorer_decl, str):
        return scorer_decl, {}
    if isinstance(scorer_decl, dict):
        name = scorer_decl.get("name")
        if not name:
            return None
        args = scorer_decl.get("args") or {}
        return name, dict(args)
    return None


def resolve_per_sample_scorers(samples: list[Any]) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}

    for sample in samples:
        sid = getattr(sample, "id", None) or "unknown"
        meta: dict = getattr(sample, "metadata", None) or {}

        decls = meta.get("scorers")
        if decls is None:
            mode = meta.get("mode")
            decls = MODE_DEFAULTS.get(mode, []) if mode else []

        resolved: list[Any] = []
        for raw in decls:
            parsed = _resolve_one(raw)
            if parsed is None:
                continue
            name, args = parsed
            factory = SCORER_REGISTRY.get(name)
            if factory is None:
                resolved.append(None)
                continue
            scorer_cb = _instantiate(factory, args)
            resolved.append(scorer_cb)

        out[sid] = [s for s in resolved if s is not None]

    return out


def _instantiate(factory: Callable[..., Any], args: dict) -> Any:
    if args:
        try:
            return factory(**args)
        except TypeError:
            pass
    try:
        return factory()
    except TypeError as e:
        _ = e  # intentionally suppressed; resolver-level miss
        return None


__all__ = [
    "SCORER_REGISTRY",
    "MODE_DEFAULTS",
    "resolve_per_sample_scorers",
]
