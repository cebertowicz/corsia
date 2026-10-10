"""Proposed usage projection: frozen lesson membership and estimated index cost.

Design-only acceptance tests until the display contract is ratified. Calls the
existing live/reconstructed usage seam; never reads current lesson files.
"""
from __future__ import annotations

import copy
import json

import pytest

from infra.context_usage_attribution import merge_context_usage_with_snapshot

SOURCE = "dynamic:skills-index"
BEHAVIOR = "a" * 64


def snapshot(*, lessons, tokens=120):
    metadata = {"provider": "skills"}
    if lessons is not None:
        metadata["lessons"] = lessons
    return {
        "snapshot_id": "ctx-frozen-history",
        "content_hash": "frozen-hash",
        "harness_model_facts": {"model": "unknown-local-model"},
        "source_inventory": [{
            "source_id": SOURCE,
            "origin_section": "dynamic",
            "inclusion": "included",
            "prompt_tokens": tokens,
            "metadata": metadata,
        }],
    }


def lesson(name="retained-expired"):
    return {
        "id": name,
        "behavioral_sha256": BEHAVIOR,
        "name": name,
        "scope": "thread",
        "scope_id": "owner",
        "inherited": True,
        "path": "/a/retained/lesson/SKILL.md",
        "review_after": "2020-01-01T00:00:00Z",
        "index_line": f"- {name}: a frozen trigger with a review date",
    }


@pytest.mark.parametrize("continuity_mode", ["fresh", "reconstructed"])
def test_projection_keeps_frozen_snapshot_and_measured_totals(continuity_mode):
    frozen = snapshot(lessons=[lesson()])
    original = copy.deepcopy(frozen)
    frozen_bytes = json.dumps(frozen, sort_keys=True)
    usage = {"total_used": 900, "token_usage_available": True}
    merged = merge_context_usage_with_snapshot(
        usage, snapshot=frozen, continuity_mode=continuity_mode,
    )
    assert frozen == original
    assert json.dumps(merged["context_snapshot"], sort_keys=True) == frozen_bytes
    assert merged["total_used"] == 900
    assert usage == {"total_used": 900, "token_usage_available": True}
    [estimate] = merged["lesson_index_estimates"]["sources"][SOURCE]
    assert estimate["id"] == "retained-expired"
    assert estimate["behavioral_sha256"] == BEHAVIOR
    assert 0 < estimate["prompt_tokens"] <= 120
    assert merged["attribution_delta_tokens"] == 780


def test_absent_membership_and_known_empty_are_distinct():
    unknown = merge_context_usage_with_snapshot(None, snapshot=snapshot(lessons=None))
    empty = merge_context_usage_with_snapshot(None, snapshot=snapshot(lessons=[]))
    assert unknown["lesson_index_estimates"]["sources"][SOURCE] is None
    assert empty["lesson_index_estimates"]["sources"][SOURCE] == []


def test_long_descriptors_are_capped_by_the_exact_skills_total():
    rows = [lesson("first"), lesson("second")]
    for row in rows:
        row["index_line"] += " long trigger" * 500
    merged = merge_context_usage_with_snapshot(None, snapshot=snapshot(lessons=rows, tokens=17))
    estimates = merged["lesson_index_estimates"]["sources"][SOURCE]
    assert {row["id"] for row in estimates} == {"first", "second"}
    assert sum(row["prompt_tokens"] for row in estimates) <= 17
    assert merged["total_used"] == 17


def test_missing_cost_does_not_erase_known_membership():
    merged = merge_context_usage_with_snapshot(None, snapshot=snapshot(lessons=[lesson()], tokens=None))
    [estimate] = merged["lesson_index_estimates"]["sources"][SOURCE]
    assert estimate["id"] == "retained-expired"
    assert estimate["behavioral_sha256"] == BEHAVIOR
    assert estimate["prompt_tokens"] is None


def test_raw_lines_and_offer_metadata_are_not_replaced_by_cost_rows():
    frozen = snapshot(lessons=[lesson()])
    merged = merge_context_usage_with_snapshot(None, snapshot=frozen)
    [estimate] = merged["lesson_index_estimates"]["sources"][SOURCE]
    assert set(estimate) == {"id", "behavioral_sha256", "prompt_tokens"}
    assert merged["context_snapshot"]["source_inventory"][0]["metadata"]["lessons"] == [lesson()]
