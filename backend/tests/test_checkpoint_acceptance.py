"""Checkpoint acceptance preserves paid output instead of judging its shape."""

from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    "body",
    (
        '{"schema_version":99,"unknown":{"keep":"exact"}}',
        "plain continuation prose",
        "{malformed json with useful state",
    ),
)
def test_every_nonempty_body_is_usable_without_parsing(body: str) -> None:
    from core.checkpoint_render import CapturedCheckpoint, usable_result

    captured = CapturedCheckpoint(
        raw_body=body,
        has_staged_edits=False,
        usage={"input_tokens": 50, "output_tokens": 7},
    )

    assert captured.usable_result()
    assert usable_result(body)
    assert captured.raw_body == body


def test_acceptance_has_no_process_or_tool_choreography_inputs() -> None:
    from core.checkpoint_render import CapturedCheckpoint

    assert {field.name for field in fields(CapturedCheckpoint)} == {
        "raw_body",
        "has_staged_edits",
        "usage",
    }
    # These provider facts are deliberately outside the acceptance value.
    provider_facts = {
        "is_error": True,
        "tool_calls": [{"name": "anything"}],
        "finish_reason": "length",
    }
    captured = CapturedCheckpoint("valid paid body", False, None)

    assert provider_facts and captured.usable_result()


def test_staged_edit_without_body_is_not_usable() -> None:
    # A blank narrative can no longer stand as the thread's whole covered
    # history now that an installed checkpoint replaces the raw archive
    # (#1767) -- staged continuity edits alone no longer earn an install.
    from core.checkpoint_render import CapturedCheckpoint, usable_result

    captured = CapturedCheckpoint("", True, {"output_tokens": 0})

    assert not captured.usable_result()
    assert not usable_result("")


@pytest.mark.parametrize("body", ("", "  ", "\n\t"))
def test_blank_body_is_never_usable_regardless_of_staged_edits(body: str) -> None:
    from core.checkpoint_render import CapturedCheckpoint, usable_result

    captured = CapturedCheckpoint(body, False, None)

    assert not captured.usable_result()
    assert not usable_result(body)


def test_render_falls_back_to_raw_snapshot_bytes_behind_an_unreadable_marker() -> None:
    """A narrative that looks malformed always passes through unchanged, and a
    snapshot that is not a JSON list at all falls back to its raw bytes behind
    an explicit unreadable marker -- both are the current contract, not new
    (see core/continuity_render.py). Byte preservation of unknown/extra body
    keys *inside* a valid list is covered by
    test_continuity_render.py::test_unknown_body_keys_are_kept.
    """
    from core.checkpoint_render import render_installed_checkpoint
    from core.continuity_render import _UNREADABLE_MARKER

    raw = ' {"unknown":true, "nested": [1,2]}\n{still malformed'
    snapshot = '{not a json list at all'
    checkpoint = SimpleNamespace(
        raw_result=raw,
        continuity_snapshot_json=snapshot,
        error=None,
        artifact_snapshot_json=None,
        plan_status_snapshot_json=None,
        through_at=None,
        captured_at=None,
    )

    rendered = render_installed_checkpoint(checkpoint)

    assert rendered == (
        "[CORSIA checkpoint narrative]\n"
        + raw
        + "\n[CORSIA continuity entries]\n"
        + _UNREADABLE_MARKER + "\n"
        + snapshot
    )
    assert raw in rendered
    assert snapshot in rendered


def test_render_is_deterministic_for_installed_and_off_contract_snapshots() -> None:
    """Determinism holds on both answers the renderer can give.

    Every stored snapshot comes from continuity_store.active_entry_snapshot,
    which always emits {entry_id, field, body, revision}; anything else is a
    corrupt row and degrades to the marked raw bytes. Both paths have to be
    byte-stable, because an unstable prefix costs the provider cache.
    """
    from core.checkpoint_render import render_installed_checkpoint

    stored = SimpleNamespace(
        raw_result="",
        continuity_snapshot_json=(
            '[{"body":{"statement":"kept","source_refs":[]},'
            '"entry_id":1,"field":"decisions","revision":1}]'
        ),
        error=None,
        artifact_snapshot_json=None,
        plan_status_snapshot_json=None,
        through_at=None,
        captured_at=None,
    )
    off_contract = SimpleNamespace(
        raw_result="",
        continuity_snapshot_json='[{"entry_id":1,"field":"decisions"}]',
        error=None,
        artifact_snapshot_json=None,
        plan_status_snapshot_json=None,
        through_at=None,
        captured_at=None,
    )

    first = render_installed_checkpoint(stored)
    degraded = render_installed_checkpoint(off_contract)

    assert first == render_installed_checkpoint(stored)
    assert degraded == render_installed_checkpoint(off_contract)
    assert first.startswith("[CORSIA checkpoint narrative]\n\n")
    assert "## decisions (index)\n#1 kept" in first
    # The off-contract row keeps its exact bytes rather than a bare header.
    assert '{"entry_id":1,"field":"decisions"}' in degraded


def test_prompt_guides_short_live_edits_and_freeform_final_narrative() -> None:
    from prompts.checkpoint import build_checkpoint_system_prompt

    prompt = build_checkpoint_system_prompt().lower()

    assert "continuity_edit" in prompt
    assert "as the conversation progressed" in prompt
    assert "final" in prompt and "narrative" in prompt
    assert "objective" in prompt and "decisions" in prompt and "known failures" in prompt
    assert "runtime metadata" in prompt and "not continuity" in prompt
    assert "do not execute or continue" in prompt
    assert "pending tool calls" in prompt
    assert "remain pending for the foreground turn" in prompt
    assert "return only one json" not in prompt
    assert "do not call tools" not in prompt


def test_replay_description_never_labels_checkpoint_validated_or_degraded() -> None:
    from core.context_replay import checkpoint_prompt

    tail = SimpleNamespace(
        start_exclusive=None,
        sealed_boundary=None,
        retention_stub_seqs=(),
        missing_result_stub_seqs=(),
    )
    description = checkpoint_prompt(SimpleNamespace(
        mode="checkpoint_tail",
        checkpoint_json="exact paid body",
        checkpoint_boundary=None,
        checkpoint_generation_id="checkpoint-1",
        tail=tail,
    )).lower()

    assert "prior thread context" in description
    assert "validated" not in description
    assert "degraded" not in description
