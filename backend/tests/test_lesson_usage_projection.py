"""Usage projection of a frozen snapshot that carries historical lesson membership.

Trial lessons are delivered per task, so no usage edge derives a cached-index
lesson estimate. A frozen snapshot recorded before that keeps its stored
lesson metadata byte for byte, and measured totals are never replaced.
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
    assert "lesson_index_estimates" not in merged
    assert merged["attribution_delta_tokens"] == 780


def test_raw_lines_and_offer_metadata_are_not_replaced_by_cost_rows():
    frozen = snapshot(lessons=[lesson()])
    merged = merge_context_usage_with_snapshot(None, snapshot=frozen)
    assert merged["context_snapshot"]["source_inventory"][0]["metadata"]["lessons"] == [lesson()]
    assert "lesson_index_estimates" not in merged
