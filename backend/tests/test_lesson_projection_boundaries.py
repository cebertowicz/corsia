"""Actual usage attachment and terminal branches preserve frozen provenance.

No edge derives a cached-index lesson estimate; a frozen snapshot that carries
historical lesson membership is attached unchanged.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from core.turn_context import _context_usage
from core.turn_iteration_context import _context_usage_from_latest_call
from infra.context_receipts import context_usage_for_boundary, context_usage_summary
from infra.context_usage_attribution import merge_context_usage_with_snapshot
from test_lesson_usage_projection import lesson, snapshot


def native_preflight(frozen):
    prefix = {
        "source_section_tokens": {"dynamic": 120}, "history_tokens": 0,
        "model_context_window": 20000, "system_tokens": 120, "max_tokens": 100,
        "was_trimmed": False, "history_messages": 0,
        "snapshot_id": frozen["snapshot_id"], "effective_context_snapshot": frozen,
        "continuity_mode": "full", "checkpoint_generation_id": None,
        "continuity_accounting": SimpleNamespace(receipt_fields=lambda: {}),
    }
    return _context_usage(
        [{"role": "user", "content": "inspect"}], "inspect", prefix, [],
        model=frozen.get("harness_model_facts", {}).get("model"),
    )


INVENTORIES = ["populated", "empty", "missing", "non-list", "minimal"]


def frozen_snapshot(inventory):
    frozen = snapshot(lessons=[lesson()])
    if inventory == "empty":
        frozen["source_inventory"] = []
    elif inventory == "missing":
        frozen.pop("source_inventory")
    elif inventory == "non-list":
        frozen["source_inventory"] = {}
    elif inventory == "minimal":
        frozen = {"snapshot_id": frozen["snapshot_id"], "content_hash": "c" * 64}
    return frozen


def assert_projection(usage, frozen=None):
    assert "lesson_index_estimates" not in usage
    if frozen is not None:
        assert usage.get("context_snapshot") == frozen


@pytest.mark.parametrize("call_usage", [{}, {"prompt_tokens": 900}])
@pytest.mark.parametrize("details", [False, True])
@pytest.mark.parametrize("inventory", INVENTORIES)
def test_native_measured_and_unmeasured_terminal_snapshot_invariant(call_usage, details, inventory):
    frozen = frozen_snapshot(inventory)
    original = copy.deepcopy(frozen)
    preflight = native_preflight(frozen)
    assert_projection(preflight, frozen)
    terminal = _context_usage_from_latest_call(
        preflight, call_usage, include_context_details=details,
    )
    assert ("context_snapshot" in terminal) == details
    assert_projection(terminal, frozen if details else None)
    if not call_usage:
        assert terminal["total_used"] is None
        assert terminal["stale_reason"] == "terminal_without_measurement"
    else:
        assert terminal["total_used"] == 900
    assert frozen == original


@pytest.mark.parametrize("edge", ["preview", "reconstructed"])
@pytest.mark.parametrize("inventory", INVENTORIES)
def test_preview_and_reconstructed_usage_derive_from_retained_dictionary(edge, inventory):
    frozen = frozen_snapshot(inventory)
    original = copy.deepcopy(frozen)
    if edge == "preview":
        usage = context_usage_for_boundary(snapshot=frozen)
    else:
        usage = merge_context_usage_with_snapshot(
            {"total_used": 900}, snapshot=frozen, continuity_mode="reconstructed",
        )
    assert_projection(usage, frozen)
    assert frozen == original
    assert "context_snapshot" not in context_usage_summary(usage)


def test_final_preview_selection_replaces_earlier_snapshot_projection():
    earlier = snapshot(lessons=[])
    selected = snapshot(lessons=[lesson()])
    selected["snapshot_id"] = "ctx-selected"
    selected["harness_model_facts"]["model"] = "selected-model"
    usage = context_usage_for_boundary(
        snapshot=earlier,
        extra={"context_snapshot": selected, "snapshot_id": "ctx-selected"},
    )
    assert_projection(usage, selected)
    assert usage["snapshot_id"] == "ctx-selected"


def test_snapshot_withheld_returns_usage_without_mutating_input():
    usage = {"total_used": 900}
    projected = merge_context_usage_with_snapshot(usage, snapshot=None)
    assert projected == {"total_used": 900}
    assert_projection(projected)
    assert usage == {"total_used": 900}


@pytest.mark.parametrize("harness", ["native", "cc", "codex"])
@pytest.mark.parametrize("inventory", INVENTORIES)
def test_real_next_turn_preview_projection_attaches_matching_provenance(monkeypatch, harness, inventory):
    from core import context_preview, context_preview_projection
    from test_context_preview_contracts import _patch_preview_sources, _preview_connection, _target

    connection, thread_row = _preview_connection()
    target = _target(harness=harness, model=f"{harness}/model")
    _patch_preview_sources(monkeypatch, target)
    export_boundary = context_preview_projection.context_usage_for_boundary
    exported = {}

    def selected_boundary(*, snapshot, **kwargs):
        frozen = snapshot.to_dict()
        if inventory == "empty":
            frozen["source_inventory"] = []
        elif inventory == "missing":
            frozen.pop("source_inventory")
        elif inventory == "non-list":
            frozen["source_inventory"] = {}
        elif inventory == "minimal":
            frozen = {"snapshot_id": frozen["snapshot_id"], "content_hash": frozen["content_hash"]}
        exported["snapshot"] = copy.deepcopy(frozen)
        return export_boundary(snapshot=frozen, **kwargs)

    monkeypatch.setattr(context_preview_projection, "context_usage_for_boundary", selected_boundary)
    try:
        usage = context_preview.build_next_turn_context_preview(
            "preview-thread", thread_row, db=connection,
        )
    finally:
        connection.close()
    assert_projection(usage, exported["snapshot"])
    assert usage["snapshot_id"] == exported["snapshot"]["snapshot_id"]
