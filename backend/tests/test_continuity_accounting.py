"""Continuity-block accounting: one outcome per turn, decided at delivery."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.continuity_accounting import (
    ContinuityAccounting,
    measure_delivered_block,
    with_continuity,
)
from core.continuity_render import CONTINUITY_ENTRIES_HEADER
from infra.context_receipts import ContextReceiptIdentity

BLOCK = CONTINUITY_ENTRIES_HEADER + "- a durable entry\n"
CHECKPOINT = "preface\n" + BLOCK
MEASURED = {
    "continuity_block_bytes": len(BLOCK.encode("utf-8")),
    "continuity_block_tokens": measure_delivered_block(CHECKPOINT, None).block_tokens,
}


def test_an_outcome_is_measured_or_one_reason_never_both() -> None:
    with pytest.raises(ValueError):
        ContinuityAccounting()
    with pytest.raises(ValueError):
        ContinuityAccounting(block_bytes=1, block_tokens=1, reason="x")
    assert ContinuityAccounting.not_delivered().receipt_fields() == {
        "continuity_block_reason": "no_checkpoint_installed",
    }
    assert ContinuityAccounting.provider_resume().receipt_fields() == {
        "continuity_block_reason": "provider_resume_unobservable",
    }
    assert ContinuityAccounting.unaccounted().receipt_fields() == {
        "continuity_block_reason": "unaccounted_context_path",
    }


def test_measurement_counts_only_the_exact_block_in_utf8_bytes() -> None:
    accounting = measure_delivered_block("préface\n" + BLOCK, None)
    assert accounting.receipt_fields() == MEASURED
    assert measure_delivered_block("no block here", None).reason == (
        "no_checkpoint_installed"
    )
    assert measure_delivered_block(None, None).reason == "no_checkpoint_installed"


def test_native_delivery_step_measures_the_prefix_checkpoint() -> None:
    from core.native_context_prefix import _prefix_info

    snapshot = SimpleNamespace(
        harness_model_facts={"model": "m"}, snapshot_id="s", content_hash="h",
        to_dict=dict,
    )

    def info(component: str) -> ContinuityAccounting:
        return _prefix_info(
            model_context=1000, system_tokens=1,
            checkpoint_component=component, tool_tokens=0, source_section_tokens={},
            history=[], replay_selection=None, snapshot=snapshot, prefix=[],
            frozen_tools=[],
        )["continuity_accounting"]

    assert info(CHECKPOINT).receipt_fields() == {
        "continuity_block_bytes": len(BLOCK.encode()),
        "continuity_block_tokens": measure_delivered_block(CHECKPOINT, "m").block_tokens,
    }
    assert info("").reason == "no_checkpoint_installed"


@pytest.fixture
def cc_frame_builder(monkeypatch):
    from subagents import cc_context_reconstruction

    monkeypatch.setattr(
        cc_context_reconstruction, "collect_context_sources",
        lambda **_kwargs: SimpleNamespace(
            installed=None, project_cwd="/workspace", sources=[], agent_facts={},
        ),
    )

    def build(**kwargs) -> ContinuityAccounting:
        return cc_context_reconstruction.build_cc_user_frame_reconstruction(
            thread_id="thread-1", user_text="next", catalog_model_id="claude-code/opus",
            model_alias="opus", working_dir="/workspace", project_id="p",
            is_subagent=False, **kwargs,
        ).continuity_accounting

    return build


def test_claude_code_delivery_step_measures_or_states_why_not(cc_frame_builder) -> None:
    assert cc_frame_builder(checkpoint_text=CHECKPOINT).block_bytes == MEASURED[
        "continuity_block_bytes"
    ]
    assert cc_frame_builder().reason == "no_checkpoint_installed"
    # A provider-opaque resume never carries the checkpoint: no bytes, even if
    # a checkpoint was available to the composer.
    opaque = cc_frame_builder(provider_handle_opaque=True, checkpoint_text=CHECKPOINT)
    assert opaque.receipt_fields() == {
        "continuity_block_reason": "provider_resume_unobservable",
    }


def _codex_payload(
    *, codex_thread_id, history_items, destination=None, checkpoint_text=None,
):
    from subagents import codex_spawn_preflight

    reconstruction = SimpleNamespace(
        turn_text="turn", task_text="task", history_items=history_items,
        checkpoint_text=checkpoint_text,
    )
    patches = [patch.object(
        codex_spawn_preflight, "build_codex_reconstruction",
        return_value=reconstruction,
    )]
    if destination is not None:
        patches.append(patch.object(
            codex_spawn_preflight, "prepared_codex_destination",
            return_value=destination,
        ))
    kwargs = dict(
        thread_id="t", text="hi", current_input_message_id=None,
        codex_thread_id=codex_thread_id, catalog_model_id="codex/m",
        context_window=1000,
        prepared_context=object() if destination is not None else None,
        working_dir="/w", project_id=None, db=None,
    )
    for entered in patches:
        entered.start()
    try:
        return codex_spawn_preflight._spawn_payload(**kwargs)
    finally:
        for entered in patches:
            entered.stop()


CHECKPOINT_ITEMS = (
    {"content": [{"type": "input_text", "text": CHECKPOINT}]},
)


def test_codex_cold_spawn_measures_the_delivered_checkpoint_text() -> None:
    payload = _codex_payload(
        codex_thread_id=None, history_items=CHECKPOINT_ITEMS,
        checkpoint_text=CHECKPOINT,
    )
    assert payload.continuity_accounting.receipt_fields() == {
        "continuity_block_bytes": MEASURED["continuity_block_bytes"],
        "continuity_block_tokens": measure_delivered_block(
            CHECKPOINT, "codex/m",
        ).block_tokens,
    }
    empty = _codex_payload(codex_thread_id=None, history_items=())
    assert empty.continuity_accounting.reason == "no_checkpoint_installed"
    replayed = _codex_payload(
        codex_thread_id=None, history_items=CHECKPOINT_ITEMS, checkpoint_text=None,
    )
    assert replayed.continuity_accounting.reason == "no_checkpoint_installed"


def test_codex_provider_resume_never_reports_an_undelivered_block() -> None:
    cold_path = _codex_payload(
        codex_thread_id="prov", history_items=CHECKPOINT_ITEMS,
        checkpoint_text=CHECKPOINT,
    )
    assert cold_path.history_items == ()
    assert cold_path.continuity_accounting.receipt_fields() == {
        "continuity_block_reason": "provider_resume_unobservable",
    }
    destination = SimpleNamespace(
        delivery_mode="provider_resume", provider_thread_id="prov",
        reconstruction=SimpleNamespace(checkpoint_text=CHECKPOINT),
        provider_text="turn",
        history_items=CHECKPOINT_ITEMS, tool_registry=object(), dynamic_tools=(),
    )
    prepared = _codex_payload(
        codex_thread_id="prov", history_items=(), destination=destination,
    )
    assert prepared.continuity_accounting.receipt_fields() == {
        "continuity_block_reason": "provider_resume_unobservable",
    }


def test_codex_live_and_terminal_receipts_carry_the_same_accounting(monkeypatch) -> None:
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime_terminal, codex_terminal_context
    from subagents.codex_runtime_state import _LiveCodex

    accounting = measure_delivered_block(CHECKPOINT, "codex/gpt-5.6-luna")
    session = SimpleNamespace(
        transport_generation_id="11111111-1111-4111-8111-111111111111",
        returncode=None, working_dir="/tmp",
        peek_active_usage=lambda: {"input_tokens": 42_000},
    )
    live = _LiveCodex(
        codex_thread_id="provider-thread", session=session, parent_thread_id=None,
        name="codex", model="gpt-5.6-luna", catalog_model_id="codex/gpt-5.6-luna",
        runtime_context_epoch=5, corsia_turn=TurnIdentity("turn-1", 1),
        continuity_accounting=accounting,
    )
    monkeypatch.setattr(
        codex_terminal_context, "merge_context_usage_with_snapshot",
        lambda usage, **_kwargs: usage,
    )

    live_receipt = codex_runtime_terminal.live_measured_context_usage("thread-1", live)
    terminal = codex_runtime_terminal.context_usage_with_snapshot(
        {"total_used": 42_000}, live,
    )

    assert live_receipt is not None and terminal is not None
    for key, value in accounting.receipt_fields().items():
        assert live_receipt[key] == terminal[key] == value


def test_claude_code_live_and_terminal_receipts_carry_the_same_accounting(
    monkeypatch,
) -> None:
    from subagents import cc_event_context_usage as usage_module

    accounting = ContinuityAccounting.provider_resume()
    live = SimpleNamespace(
        continuity_accounting=accounting, context_window=1000,
        discovered_context_window=None, context_snapshot=None,
        context_snapshot_id=None, source_artifacts=None, tool_definitions=None,
    )
    identity = ContextReceiptIdentity("claude-code", "claude-code/opus", 1, "a" * 64)
    monkeypatch.setattr(usage_module, "context_receipt_identity", lambda *_: identity)
    monkeypatch.setattr(
        usage_module, "context_usage_from_final_call",
        lambda *_args, **_kwargs: {"total_used": 10, "token_usage_available": True},
    )
    monkeypatch.setattr(
        usage_module, "merge_context_usage_with_snapshot",
        lambda usage, **_kwargs: usage,
    )

    live_receipt = usage_module.live_measured_context_usage("child", live, {})
    terminal = usage_module.context_usage_with_snapshot({"total_used": 10}, live)

    assert live_receipt is not None and terminal is not None
    assert live_receipt["continuity_block_reason"] == "provider_resume_unobservable"
    assert terminal["continuity_block_reason"] == "provider_resume_unobservable"


def test_a_receipt_without_decided_accounting_says_so() -> None:
    assert with_continuity({"total_used": 1}, None) == {
        "total_used": 1, "continuity_block_reason": "unaccounted_context_path",
    }
    assert with_continuity(None, ContinuityAccounting.unaccounted()) is None


def test_a_later_delivery_replaces_the_earlier_accounting_entirely() -> None:
    measured = ContinuityAccounting.measured(BLOCK, None)
    first = with_continuity({"total_used": 1}, measured)
    assert "continuity_block_bytes" in first
    second = with_continuity(first, ContinuityAccounting.not_delivered())
    assert second == {
        "total_used": 1, "continuity_block_reason": "no_checkpoint_installed",
    }


@pytest.mark.asyncio
async def test_a_mid_turn_checkpoint_install_replaces_the_prefix_accounting(
    monkeypatch,
) -> None:
    from core.turn_iteration import _IterationState
    from core.turn_iteration_context import _render_native_installed

    installed = ContinuityAccounting.measured(BLOCK, "native/test")
    stale = with_continuity({}, ContinuityAccounting.not_delivered())
    ledger = SimpleNamespace(
        effective_window_tokens=100_000, target_catalog_id="native/test",
        input_identity="i", rendered_input_tokens=5,
    )
    state = _IterationState(
        messages=[], context_usage=stale, tool_definitions=[], context_identity=None,
        fit_ledger=ledger, fit_current_input=None, checkpoint_source_messages=[],
        turn_model="native/test", provider_delivery_settled=True,
        active_boundary_input=None, last_measured_call_usage=None,
        last_measured_rendered_input_tokens=None,
    )
    monkeypatch.setattr(
        "core.checkpoint_resume.installed_replay",
        lambda _: SimpleNamespace(tail=SimpleNamespace(messages=())),
    )

    async def prefix(**_kwargs):
        return [], {"continuity_accounting": installed}

    monkeypatch.setattr("core.native_context_prefix.build_real_send_prefix", prefix)
    monkeypatch.setattr(
        "core.destination_preflight.attach_native_provider_fit_ledger",
        lambda **_kw: SimpleNamespace(fit_ledger=ledger),
    )
    continuation = await _render_native_installed(
        SimpleNamespace(id="replacement"), agent=object(), state=state,
        thread_id="thread-1", turn_identity=SimpleNamespace(turn_id="turn-1"),
        turn_model="native/test", project_id=None,
    )

    usage = continuation.payload.context_usage
    assert "continuity_block_reason" not in usage
    assert usage["continuity_block_bytes"] == len(BLOCK.encode("utf-8"))
    assert usage["continuity_block_tokens"] == installed.block_tokens
