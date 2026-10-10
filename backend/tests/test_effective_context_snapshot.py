from __future__ import annotations

from copy import deepcopy


def _mixed_sources():
    from core.effective_context import ContextSourceEntry

    return [
        ContextSourceEntry(
            source_id="system-agents",
            origin_section="system",
            label="AGENTS.md",
            stable_reference="workspace://AGENTS.md",
            freshness="fresh",
            inclusion="included",
            reason="workspace bootloader",
            owner_surface="workspace-settings",
            prompt_content="system instructions",
            debug_reference="/Users/alice/Documents/AGENTS.md",
            metadata={"mtime": "2026-06-30T12:00:00Z"},
        ),
        ContextSourceEntry(
            source_id="role:agent-owner-map",
            origin_section="role",
            label="Agent responsibility map",
            stable_reference="thread://sess-1/agents",
            freshness="fresh",
            inclusion="included",
            reason="stable responsibility map",
            owner_surface="agents",
            prompt_content="Agent: Release Coordinator\nResponsibility: Release sequencing",
        ),
        ContextSourceEntry(
            source_id="summary-old",
            origin_section="summary",
            label="Older session summary",
            stable_reference="summary://s-1",
            freshness="fresh",
            inclusion="summarized",
            reason="pre-window history",
            owner_surface="summary-details",
            prompt_content="summary text",
        ),
        ContextSourceEntry(
            source_id="artifact-redacted",
            origin_section="artifact",
            label="Secret artifact",
            stable_reference="artifact://a-1",
            freshness="fresh",
            inclusion="redacted",
            reason="unauthorized source",
            owner_surface="artifact-viewer",
        ),
        ContextSourceEntry(
            source_id="stale-doc",
            origin_section="project",
            label="stale.md",
            stable_reference="project://stale.md",
            freshness="stale",
            inclusion="omitted",
            reason="source changed after snapshot",
            owner_surface="project-settings",
            debug_reference="/tmp/stale.md",
        ),
        ContextSourceEntry(
            source_id="runtime-clock",
            origin_section="dynamic",
            label="Runtime clock",
            stable_reference="runtime://clock",
            freshness="fresh",
            inclusion="included",
            reason="turn runtime context",
            owner_surface="runtime",
            prompt_content="Current Time: 2026-06-30 13:30",
        ),
    ]


def test_snapshot_records_all_required_fact_classes():
    from core.effective_context import build_effective_context_snapshot
    from core.token_estimation import _estimate_tokens

    snapshot = build_effective_context_snapshot(
        thread_id="sess-1",
        reason="turn",
        scope_facts={
            "scope_id": "project-corsia",
            "scope_type": "project",
            "project_id": "corsia",
        },
        agent_facts={
            "agent_count": 1,
            "agent_thread_ids": ["agent-release"],
        },
        harness_model_facts={
            "harness": "native",
            "model": "anthropic/claude",
            "model_context_window": 200000,
        },
        cwd="/Users/alice/Documents/projects/corsia",
        source_root="/Users/alice/.corsia",
        sources=_mixed_sources(),
        history=[{"role": "user", "content": "hello"}],
        current_user_content="do the work",
        delivery_harnesses=("native", "claude-code", "codex"),
    )

    assert snapshot.thread_id == "sess-1"
    assert snapshot.reason == "turn"
    assert snapshot.scope_facts["scope_id"] == "project-corsia"
    assert snapshot.agent_facts["agent_count"] == 1
    assert snapshot.harness_model_facts["harness"] == "native"
    assert snapshot.cwd == "/Users/alice/Documents/projects/corsia"
    assert snapshot.source_root == "/Users/alice/.corsia"
    assert snapshot.snapshot_id.startswith("ctx-")
    assert len(snapshot.content_hash) == 64
    assert {leaf.harness for leaf in snapshot.delivery_leaves} == {
        "native",
        "claude-code",
        "codex",
    }

    as_dict = snapshot.to_dict()
    assert as_dict["source_inventory"][0]["source_id"] == "system-agents"
    assert as_dict["source_inventory"][0]["prompt_tokens"] == (
        _estimate_tokens("system instructions") + 4
    )
    stale_doc = next(
        source for source in as_dict["source_inventory"]
        if source["source_id"] == "stale-doc"
    )
    assert stale_doc["prompt_tokens"] is None
    assert as_dict["delivery_leaves"][0]["snapshot_id"] == snapshot.snapshot_id


def test_source_inventory_tracks_included_omitted_stale_redacted_summarized_and_dynamic_sources():
    from core.effective_context import build_effective_context_snapshot

    snapshot = build_effective_context_snapshot(
        thread_id="sess-2",
        reason="spawn",
        scope_facts={},
        agent_facts={},
        harness_model_facts={},
        cwd=None,
        source_root=None,
        sources=_mixed_sources(),
    )

    by_id = {src.source_id: src for src in snapshot.source_inventory}

    assert by_id["system-agents"].inclusion == "included"
    assert by_id["stale-doc"].inclusion == "omitted"
    assert by_id["stale-doc"].freshness == "stale"
    assert by_id["artifact-redacted"].inclusion == "redacted"
    assert by_id["summary-old"].inclusion == "summarized"
    assert by_id["runtime-clock"].origin_section == "dynamic"
    assert all(src.owner_surface for src in snapshot.source_inventory)


def test_prompt_truth_freezes_before_metadata_changes():
    from core.effective_context import build_effective_context_snapshot

    sources = _mixed_sources()
    snapshot = build_effective_context_snapshot(
        thread_id="sess-3",
        reason="turn",
        scope_facts={},
        agent_facts={},
        harness_model_facts={},
        cwd="/tmp/project",
        source_root="/tmp/project/src",
        sources=sources,
        history=[{"role": "user", "content": "original"}],
        current_user_content="current",
    )

    original_hash = snapshot.content_hash
    original_material = deepcopy(snapshot.frozen_prompt_material)

    sources[0].metadata["mtime"] = "2099-01-01T00:00:00Z"
    sources[0].prompt_content = "mutated prompt text"

    assert snapshot.content_hash == original_hash
    assert snapshot.frozen_prompt_material == original_material
    assert "mutated prompt text" not in snapshot.frozen_prompt_material["source_text"]


def test_snapshot_records_consumed_authority_without_defining_agent_or_scope_policy():
    from core.effective_context import build_effective_context_snapshot

    snapshot = build_effective_context_snapshot(
        thread_id="sess-4",
        reason="validation",
        scope_facts={
            "scope_id": "scope-1",
            "authority_slot": "project-conductor",
            "validation": "allowed",
        },
        agent_facts={
            "agent_count": 1,
            "responsibility": "review",
            "forbidden_actions": ["write_code"],
        },
        harness_model_facts={"model": "claude-code/opus"},
        cwd=None,
        source_root=None,
        sources=[],
    )

    assert snapshot.scope_facts["authority_slot"] == "project-conductor"
    assert snapshot.agent_facts["forbidden_actions"] == ["write_code"]
    assert "policy_engine" not in snapshot.to_dict()
    assert "scope_definitions" not in snapshot.to_dict()


def test_snapshot_hash_stable_for_same_inputs():
    from core.effective_context import build_effective_context_snapshot

    kwargs = dict(
        thread_id="sess-5",
        reason="turn",
        scope_facts={"scope_id": "project"},
        agent_facts={"agent_count": 1},
        harness_model_facts={"model": "native/qwen"},
        cwd="/tmp/project",
        source_root="/tmp/project/src",
        sources=_mixed_sources(),
        history=[{"role": "user", "content": "same"}],
        current_user_content="same current",
        delivery_harnesses=("native", "claude-code"),
    )

    first = build_effective_context_snapshot(**kwargs)
    second = build_effective_context_snapshot(**kwargs)

    assert first.snapshot_id == second.snapshot_id
    assert first.content_hash == second.content_hash


def test_provider_source_artifact_hashes_provider_bound_payload():
    from core.effective_context import (
        build_effective_context_snapshot,
        provider_source_artifact,
    )

    snapshot = build_effective_context_snapshot(
        thread_id="sess-proof",
        reason="turn",
        scope_facts={"project_id": "corsia"},
        agent_facts={"agent_count": 1},
        harness_model_facts={"harness": "native", "model": "anthropic/test"},
        cwd="/tmp/project",
        source_root="/tmp/project",
        sources=_mixed_sources(),
        delivery_harnesses=("native",),
    )
    payload = {"messages": [{"role": "system", "content": "frozen"}]}

    artifact = provider_source_artifact(
        harness="native",
        method="native-prefix",
        snapshot_id=snapshot.snapshot_id,
        payload=payload,
    )

    assert artifact["kind"] == "provider-bound-payload"
    assert artifact["harness"] == "native"
    assert artifact["method"] == "native-prefix"
    assert artifact["snapshot_id"] == snapshot.snapshot_id
    assert len(artifact["content_hash"]) == 64
    assert artifact["payload"] == payload
