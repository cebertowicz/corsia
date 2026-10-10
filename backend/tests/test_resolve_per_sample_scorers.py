
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from eval.scorers.resolve import (
    MODE_DEFAULTS,
    SCORER_REGISTRY,
    resolve_per_sample_scorers,
)


def _sample(sid: str, *, scorers=None, mode=None, extra=None) -> object:
    meta: dict = {}
    if scorers is not None:
        meta["scorers"] = scorers
    if mode is not None:
        meta["mode"] = mode
    if extra:
        meta.update(extra)
    return SimpleNamespace(id=sid, metadata=meta)


def test_empty_samples_returns_empty_dict():
    assert resolve_per_sample_scorers([]) == {}


def test_explicit_scorers_string_form():
    s = _sample("e1", scorers=["seam_latency", "null_tool_paired"])
    out = resolve_per_sample_scorers([s])
    assert "e1" in out
    assert len(out["e1"]) == 2


def test_explicit_scorers_dict_form_with_args():
    s = _sample("e1", scorers=[
        {"name": "seam_latency", "args": {"budget_ms": 50.0}},
    ])
    out = resolve_per_sample_scorers([s])
    assert len(out["e1"]) == 1


def test_unknown_scorer_filtered_out():
    s = _sample("e1", scorers=["does_not_exist", "null_tool_paired"])
    out = resolve_per_sample_scorers([s])
    assert len(out["e1"]) == 1


def test_mode_defaults_applied_when_no_explicit_scorers():
    s = _sample("e1", mode="latency_tool_loops")
    out = resolve_per_sample_scorers([s])
    expected = MODE_DEFAULTS["latency_tool_loops"]
    assert len(out["e1"]) == len(expected)


def test_explicit_scorers_override_mode_defaults():
    s = _sample("e1", mode="latency_tool_loops", scorers=["null_tool_paired"])
    out = resolve_per_sample_scorers([s])
    assert len(out["e1"]) == 1


def test_functional_behavior_mode_no_defaults():
    s = _sample("e1", mode="functional_behavior")
    out = resolve_per_sample_scorers([s])
    assert out["e1"] == []


def test_functional_behavior_with_explicit_scorers_resolves():
    s = _sample(
        "E2",
        mode="functional_behavior",
        scorers=[
            {"name": "check_event_suppressed", "args": {"event": "turn_cancelled"}},
            "null_tool_paired",
        ],
    )
    out = resolve_per_sample_scorers([s])
    assert len(out["E2"]) == 1


def test_sample_without_metadata_yields_empty_scorers():
    s = SimpleNamespace(id="e1", metadata=None)
    out = resolve_per_sample_scorers([s])
    assert out["e1"] == []


def test_multiple_samples_isolated_resolution():
    s1 = _sample("a", scorers=["seam_latency"])
    s2 = _sample("b", mode="output_stability")
    out = resolve_per_sample_scorers([s1, s2])
    assert "a" in out and "b" in out
    assert len(out["a"]) == 1
    assert len(out["b"]) == 2


def test_registered_names_match_documented_set():
    for required in ("seam_latency", "null_tool_paired", "structured_validity"):
        assert required in SCORER_REGISTRY
        assert SCORER_REGISTRY[required] is not None


def test_sprint3_slice2_scorer_bodies_registered():
    sprint3_scorers = (
        "check_event_present",
        "check_event_metadata_relation",
        "check_no_provider_error",
        "check_assistant_message_content",
        "check_history_contains",
        "check_event_iteration_window",
    )
    for name in sprint3_scorers:
        assert name in SCORER_REGISTRY, f"{name} missing from registry"
        assert SCORER_REGISTRY[name] is not None, (
            f"{name} is still a None stub — Sprint-3 Slice-2 expected to wire it"
        )


def test_sprint4_slice6_mode_b_scorer_bodies_registered():
    sprint4_scorers = (
        "hit_rate_per_turn",
        "system_prompt_stable",
        "ttft_under",
    )
    for name in sprint4_scorers:
        assert name in SCORER_REGISTRY, f"{name} missing from registry"
        assert SCORER_REGISTRY[name] is not None, (
            f"{name} is None — Sprint-4 Slice-6 expected to wire it"
        )


def test_factory_with_unknown_kwargs_is_skipped_not_crashed():
    s = _sample("e1", scorers=[
        {"name": "check_event_present", "args": {"bogus_arg": "x"}},
        "null_tool_paired",
    ])
    out = resolve_per_sample_scorers([s])
    assert len(out["e1"]) == 1


def test_factory_with_no_args_when_required_arg_missing_skips():
    s = _sample("e1", scorers=[
        {"name": "check_event_present", "args": {}},
        "null_tool_paired",
    ])
    out = resolve_per_sample_scorers([s])
    assert len(out["e1"]) == 1
