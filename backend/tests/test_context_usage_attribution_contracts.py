from __future__ import annotations

import pytest


def _snapshot(*, opaque_history: bool = False) -> dict:
    material = {}
    if opaque_history:
        material = {
            "current_user_content": "continue",
            "provider_payload": {
                "history_source_attribution": "provider_handle_opaque",
            },
        }
    return {
        "snapshot_id": "ctx-attribution",
        "harness_model_facts": {"model_context_window": 200_000},
        "source_inventory": [{
            "source_id": "system",
            "origin_section": "system",
            "inclusion": "included",
            "prompt_tokens": 4_200,
        }],
        "frozen_prompt_material": material,
    }


def test_provider_measurement_stays_authoritative_and_reports_gap():
    from infra.context_usage_attribution import merge_context_usage_with_snapshot

    snapshot = {
        **_snapshot(),
        "source_inventory": [
            {
                "source_id": "system",
                "origin_section": "system",
                "inclusion": "included",
                "prompt_tokens": 100,
            },
            {
                "source_id": "project",
                "origin_section": "project",
                "inclusion": "included",
                "prompt_tokens": 200,
            },
        ],
    }

    merged = merge_context_usage_with_snapshot(
        {"model_context_window": 200_000, "total_used": 1_000},
        snapshot=snapshot,
    )

    assert merged["total_used"] == 1_000
    assert merged["token_source"] == "measured"
    assert merged["token_usage_available"] is True
    assert merged["system_tokens"] == 100
    assert merged["context_source_tokens"] == 200
    assert merged["attribution_delta_tokens"] == 700


@pytest.mark.parametrize(
    ("usage", "expected_total", "expected_source"),
    [
        (None, 4_200, "estimated"),
        (
            {
                "model_context_window": 200_000,
                "total_used": 4_000,
                "token_source": "estimated",
            },
            4_200,
            "estimated",
        ),
        (
            {
                "model_context_window": 200_000,
                "total_used": None,
                "token_usage_available": False,
                "stale_reason": "terminal_without_measurement",
            },
            None,
            None,
        ),
    ],
)
def test_unmeasured_usage_never_becomes_measured(
    usage: dict | None,
    expected_total: int | None,
    expected_source: str | None,
):
    from infra.context_usage_attribution import merge_context_usage_with_snapshot

    merged = merge_context_usage_with_snapshot(usage, snapshot=_snapshot())

    assert merged["total_used"] == expected_total
    assert merged["token_usage_available"] is False
    assert merged.get("token_source") == expected_source


def test_opaque_provider_history_requires_provider_measurement():
    from infra.context_usage_attribution import merge_context_usage_with_snapshot

    snapshot = _snapshot(opaque_history=True)
    unknown = merge_context_usage_with_snapshot(
        {"model_context_window": 200_000},
        snapshot=snapshot,
    )
    measured = merge_context_usage_with_snapshot(
        {
            "model_context_window": 200_000,
            "effective_input_tokens": 123_456,
            "token_usage_available": True,
        },
        snapshot=snapshot,
    )

    assert unknown["history_source_attribution"] == "provider_handle_opaque"
    assert unknown["total_used"] is None
    assert unknown["token_usage_available"] is False
    assert measured["total_used"] == 123_456
    assert measured["token_source"] == "measured"


def test_runtime_window_precedes_snapshot_window():
    from infra.context_usage_attribution import merge_context_usage_with_snapshot

    snapshot = {
        **_snapshot(),
        "harness_model_facts": {"model_context_window": 1_000_000},
        "source_inventory": [],
    }
    merged = merge_context_usage_with_snapshot(
        {
            "model_context_window": 200_000,
            "provider_reported_model_context_window": 200_000,
            "total_used": 50_000,
        },
        snapshot=snapshot,
    )

    assert merged["model_context_window"] == 200_000
    assert merged["utilization"] == 0.25


def test_snapshot_growth_recomputes_provider_utilization():
    from infra.context_usage_attribution import merge_context_usage_with_snapshot

    snapshot = {
        **_snapshot(),
        "source_inventory": [],
    }
    merged = merge_context_usage_with_snapshot(
        {
            "model_context_window": 100_000,
            "total_used": 50_000,
            "utilization": 0.5,
        },
        snapshot=snapshot,
    )

    assert merged["model_context_window"] == 200_000
    assert merged["total_used"] == 50_000
    assert merged["utilization"] == 0.25


def test_native_attribution_gap_can_be_negative():
    from infra.context_usage_attribution import merge_context_usage_with_snapshot

    merged = merge_context_usage_with_snapshot(
        {"model_context_window": 200_000, "total_used": 4_000},
        snapshot={
            **_snapshot(),
            "harness_model_facts": {
                "harness": "native",
                "model_context_window": 200_000,
            },
        },
    )

    assert merged["attribution_delta_tokens"] == -200
    assert merged["attribution_delta_reason"] == "native framing/tokenizer variance"


def test_preview_proof_and_unknown_sections_do_not_enter_attribution():
    from infra.context_usage_attribution import merge_context_usage_with_snapshot

    snapshot = {
        **_snapshot(),
        "source_inventory": [
            {
                "source_id": "system",
                "origin_section": "system",
                "inclusion": "included",
                "prompt_tokens": 100,
            },
            {
                "source_id": "preview",
                "origin_section": "preflight",
                "inclusion": "preview",
                "prompt_tokens": 500,
            },
            {
                "source_id": "proof",
                "origin_section": "system",
                "inclusion": "proof",
                "prompt_tokens": 700,
            },
            {
                "source_id": "unknown",
                "origin_section": "new-unreviewed-section",
                "inclusion": "included",
                "prompt_tokens": 9_000,
            },
        ],
    }

    merged = merge_context_usage_with_snapshot(
        {"model_context_window": 200_000, "total_used": 1_000},
        snapshot=snapshot,
    )

    assert merged["system_tokens"] == 100
    assert merged["context_source_tokens"] == 0
    assert merged["attribution_delta_tokens"] == 900


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_mcp_tool_definitions_are_carved_out_of_the_harness_row(harness: str):
    """Issue #1817: CC/Codex report tool_def_tokens=0 with no source for it, so the
    whole tool-definition cost used to hide inside attribution_delta_tokens. The
    estimate must land in tool_def_tokens and shrink the delta by the same amount,
    so rows-plus-delta still sum to exactly the measured total."""
    from infra.context_usage_attribution import (
        _mcp_tool_definition_tokens,
        merge_context_usage_with_snapshot,
    )

    known_definitions = ({"type": "function", "function": {"name": "read_file"}},)
    snapshot = {
        **_snapshot(),
        "harness_model_facts": {"harness": harness, "model_context_window": 200_000},
    }

    merged = merge_context_usage_with_snapshot(
        {"model_context_window": 200_000, "total_used": 100_000},
        snapshot=snapshot,
        known_tool_definitions=known_definitions,
    )

    expected_tool_def = _mcp_tool_definition_tokens(None, harness, known_definitions)
    assert expected_tool_def > 0
    assert merged["tool_def_tokens"] == expected_tool_def
    assert merged["attribution_delta_tokens"] == 100_000 - 4_200 - expected_tool_def
    assert merged["attribution_delta_reason"] == "unobservable harness/provider overhead"
    assert merged["total_used"] == (
        merged["system_tokens"] + merged["tool_def_tokens"] + merged["history_tokens"]
        + merged["user_tokens"] + merged["context_source_tokens"]
        + merged["attribution_delta_tokens"]
    )


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_mcp_tool_definitions_match_the_harness_own_fit_serialization(harness: str):
    """The display estimate must be byte-identical to what the harness's own
    destination-fit code already computes for the same tuple (cc_destination's
    _fit_sections 'tools' section / codex_destination's fit 'tools' term), so
    the sheet can never disagree with the real budget for the same session."""
    import json as _json

    from infra.context_usage_attribution import _mcp_tool_definition_tokens
    from core.token_estimation import _text_tokens

    definitions = (
        {"type": "function", "function": {"name": "read_file", "parameters": {}}},
        {"type": "function", "function": {"name": "grep", "parameters": {}}},
    )
    if harness == "claude-code":
        expected_text = _json.dumps(
            definitions, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, default=str,
        )
    else:
        expected_text = _json.dumps(definitions, default=str)

    assert _mcp_tool_definition_tokens(None, harness, definitions) == _text_tokens(
        expected_text, None,
    )


def test_mcp_tool_definitions_unavailable_without_a_known_live_ledger():
    """When the live session has not frozen its tool definitions yet, the
    estimate must say so (None) rather than rebuild a registry against the
    global workspace, which could disagree with what the harness actually
    received and would block the async per-turn merge path to do it."""
    from infra.context_usage_attribution import _mcp_tool_definition_tokens

    assert _mcp_tool_definition_tokens(None, "claude-code", None) is None


def test_mcp_tool_definition_failure_is_visible_in_the_delta_reason(monkeypatch):
    """A failed estimate must not look identical to 'this harness has no
    CORSIA tool definitions' — CC/Codex always have some, so a silent 0 here
    would hide a real error behind the same reason text as the normal case."""
    import infra.context_usage_attribution as attribution

    def _boom(_harness, _definitions) -> str:
        raise RuntimeError("serialization failed")

    monkeypatch.setattr(attribution, "_mcp_tool_definition_json", _boom)

    snapshot = {
        **_snapshot(),
        "harness_model_facts": {"harness": "codex", "model_context_window": 200_000},
    }

    merged = attribution.merge_context_usage_with_snapshot(
        {"model_context_window": 200_000, "total_used": 100_000},
        snapshot=snapshot,
        known_tool_definitions=({"type": "function", "function": {"name": "read_file"}},),
    )

    assert merged["tool_def_tokens"] == 0
    assert "tool-definition estimate failed" in merged["attribution_delta_reason"]
    assert merged["attribution_delta_tokens"] == 100_000 - 4_200


def test_mcp_tool_definitions_do_not_fire_for_unlisted_or_native_harness():
    from infra.context_usage_attribution import merge_context_usage_with_snapshot

    for harness in ("", "native", "some-future-harness"):
        merged = merge_context_usage_with_snapshot(
            {"model_context_window": 200_000, "total_used": 100_000},
            snapshot={
                **_snapshot(),
                "harness_model_facts": {"harness": harness, "model_context_window": 200_000},
            },
        )
        assert merged["tool_def_tokens"] == 0, harness


def test_mcp_tool_definitions_do_not_fire_without_a_measured_total():
    from infra.context_usage_attribution import merge_context_usage_with_snapshot

    merged = merge_context_usage_with_snapshot(
        {"model_context_window": 200_000, "total_used": 4_000, "token_source": "estimated"},
        snapshot={
            **_snapshot(),
            "harness_model_facts": {"harness": "claude-code", "model_context_window": 200_000},
        },
    )

    assert merged["tool_def_tokens"] == 0


def test_mcp_tool_definitions_reuse_the_live_session_ledger():
    """The estimate must be bound to the exact tool set the harness actually
    received (the live session's frozen definitions), never a rebuild against
    the global workspace that could disagree with it."""
    import infra.context_usage_attribution as attribution

    known_definitions = ({"type": "function", "function": {"name": "only_tool"}},)
    snapshot = {
        **_snapshot(),
        "harness_model_facts": {"harness": "claude-code", "model_context_window": 200_000},
    }

    merged = attribution.merge_context_usage_with_snapshot(
        {"model_context_window": 200_000, "total_used": 100_000},
        snapshot=snapshot,
        known_tool_definitions=known_definitions,
    )

    expected_tokens = attribution._mcp_tool_definition_tokens(
        None, "claude-code", known_definitions,
    )
    assert merged["tool_def_tokens"] == expected_tokens


def test_mcp_tool_definitions_never_overwrite_a_real_tool_source():
    from infra.context_usage_attribution import merge_context_usage_with_snapshot

    snapshot = {
        **_snapshot(),
        "harness_model_facts": {"harness": "claude-code", "model_context_window": 200_000},
        "source_inventory": [
            *_snapshot()["source_inventory"],
            {
                "source_id": "cc:tool-definitions",
                "origin_section": "tools",
                "inclusion": "included",
                "prompt_tokens": 55,
            },
        ],
    }

    merged = merge_context_usage_with_snapshot(
        {"model_context_window": 200_000, "total_used": 100_000},
        snapshot=snapshot,
    )

    assert merged["tool_def_tokens"] == 55
