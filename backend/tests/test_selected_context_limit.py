"""Selected model limits survive provider telemetry and context attribution."""

from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("selected", (131_072, 262_144, 393_216))
@pytest.mark.parametrize("reported", (None, 1_000_000))
def test_codex_live_and_terminal_projection_keep_selected_limit(selected, reported):
    from subagents.codex_terminal_context import (
        context_usage_with_snapshot,
        provider_context_usage,
    )

    raw = {"input_tokens": selected + 20, "cached_input_tokens": 100}
    if reported is not None:
        raw["model_context_window"] = reported
    measured = provider_context_usage(raw, selected_context_window=selected)
    assert measured["model_context_window"] == selected
    assert measured["total_used"] == selected + 20
    assert measured["utilization"] > 1
    assert measured.get("provider_reported_model_context_window") == reported
    live = SimpleNamespace(context_snapshot={
        "harness_model_facts": {"model_context_window": selected},
        "source_inventory": [], "frozen_prompt_material": {},
    }, continuity_accounting=None)
    merged = context_usage_with_snapshot(measured, live)
    assert merged["model_context_window"] == selected
    assert merged["total_used"] == selected + 20
    assert merged["utilization"] is not None
    assert merged.get("provider_reported_model_context_window") == reported


@pytest.mark.parametrize("selected", (131_072, 262_144, 393_216))
def test_external_boundary_measurement_keeps_selected_limit(selected):
    from subagents.harness_checkpoint_context import external_measurement

    live = SimpleNamespace(
        context_window=selected,
        session=SimpleNamespace(peek_active_usage=lambda: {
            "input_tokens": selected + 20, "model_context_window": 1_000_000,
        }),
    )
    assert external_measurement(live, "codex") == (selected + 20, selected)


@pytest.mark.parametrize("selected", (131_072, 262_144, 393_216))
def test_raw_provider_window_does_not_replace_selected_snapshot_limit(selected):
    from infra.context_usage_attribution import merge_context_usage_with_snapshot

    merged = merge_context_usage_with_snapshot(
        {"model_context_window": 1_000_000, "total_used": 200_000,
         "provider_reported_model_context_window": 1_000_000},
        snapshot={"harness_model_facts": {"model_context_window": selected}},
    )
    assert merged["model_context_window"] == selected
    assert merged["provider_reported_model_context_window"] == 1_000_000
    assert merged["total_used"] == 200_000


@pytest.mark.parametrize("selected,reported,expected", (
    (None, 1_000_000, 1_000_000),
    (393_216, 200_000, 200_000),
    (262_144, None, 262_144),
))
def test_telemetry_keeps_physical_ceiling_and_missing_evidence(selected, reported, expected):
    from subagents.codex_terminal_context import provider_context_usage

    projected = provider_context_usage(
        {"input_tokens": 100, "model_context_window": reported},
        selected_context_window=selected,
    )
    assert projected["model_context_window"] == expected


def test_claude_displays_actual_occupancy_above_an_artificial_limit():
    from subagents.cc_event_context_usage import context_usage_from_final_call

    measured = context_usage_from_final_call(
        {"input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 20},
        selected_context_window=800,
    )
    assert measured["total_used"] == 1000
    assert measured["model_context_window"] == 800
    assert measured["utilization"] == 1.25


def test_claude_row_carries_the_provider_capacity_beside_the_budget():
    # #1753: the backend was already honest about occupancy above the limit,
    # but the row could not say WHAT the limit was, so the widget labelled a
    # CORSIA budget "window" and the count contradicted it.
    from subagents.cc_event_context_usage import context_usage_from_final_call

    measured = context_usage_from_final_call(
        {"input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 20},
        selected_context_window=800,
        discovered_context_window=1_000_000,
    )
    assert measured["model_context_window"] == 800
    assert measured["discovered_context_window"] == 1_000_000


def test_codex_row_carries_the_provider_capacity_beside_the_budget():
    from subagents.codex_terminal_context import provider_context_usage

    measured = provider_context_usage(
        {"input_tokens": 300_000, "cached_input_tokens": 100},
        selected_context_window=262_144,
        discovered_context_window=1_000_000,
    )
    assert measured["model_context_window"] == 262_144
    assert measured["discovered_context_window"] == 1_000_000


@pytest.mark.parametrize("absent", (None, 0, -1, True))
def test_rows_report_an_unknown_provider_capacity_as_absent(absent):
    # Native sends no discovered window; absence must read as "no cap in
    # force", never as a zero the surface could divide or compare against.
    from subagents.cc_event_context_usage import context_usage_from_final_call

    measured = context_usage_from_final_call(
        {"input_tokens": 100, "cache_read_input_tokens": 0},
        selected_context_window=800,
        discovered_context_window=absent,
    )
    assert measured["discovered_context_window"] is None
