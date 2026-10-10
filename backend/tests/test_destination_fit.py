from __future__ import annotations

import pytest


SECTIONS = {
    "fixed": 100,
    "checkpoint": 40,
    "tail": 30,
    "current_input": 20,
    "tools": 10,
    "provider_wrapper": 5,
}


def _components():
    return {
        "fixed": {"system": "SYSTEM", "project": "PROJECT", "runtime": "RUNTIME"},
        "checkpoint": {"objective": "continue"},
        "tail": [{"role": "assistant", "content": "prior"}],
        "current_input": {"role": "user", "content": "FIFO_HEAD"},
        "tools": [{"name": "lookup"}],
    }


def _rendered_destination_payload():
    from core.context_payload import RenderedDestinationPayload

    return RenderedDestinationPayload(
        payload={"wire": "frozen"},
        input_tokens=sum(SECTIONS.values()),
        accounting_payload={"wire": "frozen"},
        fit_section_tokens=SECTIONS,
        fit_input_identity="fifo-head",
        fit_current_input="FIFO_HEAD",
        fit_components=_components(),
    )


def _model_switch_fit_input(*, identity: str, content: str, media_projection=None):
    from core.message_history import render_agent_envelope
    from infra.input_render import InputProviderProjection

    return InputProviderProjection(
        identity=identity,
        content=content,
        source_content=content,
        agent_envelope=render_agent_envelope(content, None),
        attachments=(),
        media_projection=media_projection,
        receipts=(),
    )


@pytest.mark.parametrize("harness", ["native", "claude-code", "codex"])
def test_destination_fit_uses_renderer_total_once_across_harnesses(harness):
    from core.destination_fit import build_destination_fit_ledger

    components = _components()
    renderer_total = sum(SECTIONS.values())
    ledger = build_destination_fit_ledger(
        harness=harness,
        target_catalog_id=f"{harness}/model",
        proven_target_window_tokens=80_000,
        accounting_payload={"provider": harness, **components},
        rendered_input_tokens=renderer_total,
        section_tokens=SECTIONS,
        input_identity="message-7",
        current_input=components["current_input"],
        components=components,
    )

    assert ledger.fits is True
    assert ledger.rendered_input_tokens == renderer_total
    assert sum(value for _name, value in ledger.section_tokens) == renderer_total
    assert dict(ledger.section_tokens) == SECTIONS
    assert ledger.input_identity == "message-7"


@pytest.mark.parametrize(("window", "fits"), [(sum(SECTIONS.values()), True), (sum(SECTIONS.values()) - 1, False)])
def test_destination_fit_blocks_only_when_rendered_input_exceeds_window(window, fits):
    from core.destination_fit import build_destination_fit_ledger

    ledger = build_destination_fit_ledger(
        harness="native",
        target_catalog_id="native/model",
        proven_target_window_tokens=window,
        accounting_payload=_components(),
        rendered_input_tokens=sum(SECTIONS.values()),
        section_tokens=SECTIONS,
        input_identity="fifo-head",
        current_input="head",
        components=_components(),
    )

    assert ledger.fits is fits


def test_destination_fit_rejects_section_sum_mismatch():
    from core.destination_fit import build_destination_fit_ledger

    with pytest.raises(ValueError, match="section sum"):
        build_destination_fit_ledger(
            harness="native",
            target_catalog_id="native/model",
            proven_target_window_tokens=80_000,
            accounting_payload=_components(),
            rendered_input_tokens=sum(SECTIONS.values()) + 1,
            section_tokens=SECTIONS,
            input_identity="fifo-head",
            current_input="head",
            components=_components(),
        )


def test_a_large_proven_target_renders_to_its_own_full_window():
    """The model's window is the window — no cap sits above it.

    A 262,144-token "portability window" used to cap EVERY render so a later
    switch to a smaller model would never wait. That checkpointed a
    503,808-token model at 52% of its real capacity. Removed 2026-08-11: a
    switch folds at switch time on the model being left, and a fold that still
    cannot fit the destination declares the truncation to the user and model.
    """
    from core.destination_fit import build_destination_fit_ledger

    ledger = build_destination_fit_ledger(
        harness="native",
        target_catalog_id="native/large",
        proven_target_window_tokens=1_000_000,
        accounting_payload=_components(),
        rendered_input_tokens=sum(SECTIONS.values()),
        section_tokens=SECTIONS,
        input_identity="head-1",
        current_input="head",
        components=_components(),
    )

    assert ledger.effective_window_tokens == 1_000_000


@pytest.mark.parametrize("window", [None, 0, -1, True])
def test_an_unproven_target_window_renders_to_our_own_bound_and_sends(window):
    """Missing window evidence is OUR discovery defect, never a local stop.

    It used to manufacture one: no catalog value meant ``fits=False`` and a
    SafePause, so a row we failed to populate refused the user's message. The
    payload is now measured against the portability window every destination is
    already capped at — the one bound we own — and the provider, which owns its
    real limit, is the only party that may reject it. The ledger stays honest:
    nothing was proved, so ``proven_target_window_tokens`` stays None.
    """
    from core.destination_fit import (
        UNPROVEN_WINDOW_FALLBACK_TOKENS,
        build_destination_fit_ledger,
    )

    ledger = build_destination_fit_ledger(
        harness="codex",
        target_catalog_id="codex/unknown",
        proven_target_window_tokens=window,
        accounting_payload=_components(),
        rendered_input_tokens=sum(SECTIONS.values()),
        section_tokens=SECTIONS,
        input_identity="fifo-head",
        current_input="head",
        components=_components(),
    )

    assert ledger.fits is True
    assert ledger.block_reason is None
    assert ledger.proven_target_window_tokens is None
    assert ledger.effective_window_tokens == UNPROVEN_WINDOW_FALLBACK_TOKENS


def test_destination_fit_payload_hash_is_identity_not_token_authority():
    from core.destination_fit import build_destination_fit_ledger

    first = build_destination_fit_ledger(
        harness="codex",
        target_catalog_id="codex/model",
        proven_target_window_tokens=80_000,
        accounting_payload={"wire": "first"},
        rendered_input_tokens=sum(SECTIONS.values()),
        section_tokens=SECTIONS,
        input_identity="fifo-head",
        current_input="head",
        components=_components(),
    )
    second = build_destination_fit_ledger(
        harness="codex",
        target_catalog_id="codex/model",
        proven_target_window_tokens=80_000,
        accounting_payload={"wire": "a different serialization"},
        rendered_input_tokens=sum(SECTIONS.values()),
        section_tokens=SECTIONS,
        input_identity="fifo-head",
        current_input="head",
        components=_components(),
    )

    assert first.destination_payload_sha256 != second.destination_payload_sha256
    assert first.rendered_input_tokens == second.rendered_input_tokens


def test_destination_fit_ledger_cannot_be_reattached():
    from core.context_payload import PreparedContextPayload
    from core.destination_preflight import attach_destination_fit_ledger

    prepared = PreparedContextPayload(
        mode="exact_replay",
        target_catalog_id="native/model",
        model_context_window=80_000,
        history_epoch=1,
        sealed_boundary=None,
        replay=None,
        rendered=_rendered_destination_payload(),
    )
    attached = attach_destination_fit_ledger(
        prepared,
        harness="native",
        proven_target_window_tokens=80_000,
    )

    with pytest.raises(ValueError, match="already has.*fit ledger"):
        attach_destination_fit_ledger(
            attached,
            harness="native",
            proven_target_window_tokens=80_000,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "harness"),
    [("native", "native"), ("cc", "claude-code"), ("codex", "codex")],
)
async def test_renderer_seam_attaches_exactly_one_ledger(
    monkeypatch,
    provider,
    harness,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from core import destination_preflight
    from core.media_projection import ValidatedImage, ValidatedMediaProjection
    import infra.destination_payloads as destination_payloads
    import infra.preflight_render as preflight_render
    from infra import send_service, send_preflight

    target = SimpleNamespace(
        harness=harness,
        catalog_id=f"{harness}/model",
        transport_model="test-model",
        expected_reported_model=None,
        context_window=80_000,
    )
    selection = SimpleNamespace(
        mode="exact",
        history_epoch=1,
        sealed_boundary=None,
        checkpoint_json="checkpoint",
        tail="tail",
    )

    async def immediate(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr("infra.db_async.run_in_db", immediate)
    if provider == "native":
        prepared_native = SimpleNamespace(
            messages=[{"role": "system", "content": "fixed"}, "FIFO_HEAD"],
            tool_definitions=[],
            context_usage={
                "rendered_input_tokens": sum(SECTIONS.values()),
                "user_tokens": SECTIONS["current_input"],
                "source_section_tokens": {},
                "fit_section_tokens": SECTIONS,
            },
        )
        monkeypatch.setattr(
            "core.turn_context.prepare_native_context_payload",
            AsyncMock(return_value=prepared_native),
        )
        monkeypatch.setattr(
            "ws.turn_helpers.build_agent_content",
            lambda content, _attachments: (content, None),
        )
    elif provider == "cc":
        destination = SimpleNamespace(
            input_tokens=sum(SECTIONS.values()),
            source_tokens=0,
            source_section_tokens={},
            accounting_payload={"wire": "cc"},
            fit_section_tokens=SECTIONS,
            frame_reconstruction=SimpleNamespace(turn_text="FIFO_HEAD"),
            seed_reconstruction=None,
            live_handle_fingerprint="fixed",
            checkpoint_text="checkpoint",
            transcript=SimpleNamespace(content="tail"),
            tool_definitions=[],
        )
        monkeypatch.setattr(
            "subagents.cc_runtime.prepare_cc_destination_payload",
            lambda *_args, **_kwargs: destination,
        )
    else:
        destination = SimpleNamespace(
            input_tokens=sum(SECTIONS.values()),
            source_tokens=0,
            source_section_token_map={},
            accounting_payload={"wire": "codex"},
            fit_section_tokens=SECTIONS,
            current_user_text="FIFO_HEAD",
            provider_text="fixed",
            history_items=["tail"],
            dynamic_tools=[],
        )
        monkeypatch.setattr(
            destination_payloads,
            "_prepare_codex_preflight_destination",
            lambda *_args, **_kwargs: destination,
        )

    attach = MagicMock(wraps=destination_preflight.attach_destination_fit_ledger)
    monkeypatch.setattr(destination_preflight, "attach_destination_fit_ledger", attach)
    media_projection = ValidatedMediaProjection((
        ValidatedImage("image-1", "image/png", b"image", "a" * 64, 5),
    ), ("a" * 64,))
    prepared = await preflight_render.render_preflight_selection(
        thread_id="thread-1",
        thread_row={"runtime_context_epoch": 0},
        provider=provider,
        target=target,
        current_user_content="FIFO_HEAD",
        current_user_source_content="FIFO_HEAD",
        attachments=[],
        media_projection=media_projection,
        selection=selection,
        live_handle=False,
        input_identity="fifo-head",
    )

    attach.assert_called_once()
    assert prepared.fit_ledger is not None
    assert prepared.fit_ledger.input_identity == "fifo-head"
    expected_media_tokens = 0 if provider == "native" else 1_000
    assert prepared.fit_ledger.rendered_input_tokens == (
        sum(SECTIONS.values()) + expected_media_tokens
    )
    assert dict(prepared.fit_ledger.section_tokens)["current_input"] == (
        SECTIONS["current_input"] + expected_media_tokens
    )


def test_checkpoint_generation_proof_binds_prompt_output_and_existing_margin():
    from core.destination_fit import prove_checkpoint_generation_fit

    proof = prove_checkpoint_generation_fit(
        target_catalog_id="codex/gpt",
        proven_target_window_tokens=32_000,
        serialized_request='{"prompt":"frozen"}',
        rendered_input_tokens=2_000,
        constrained_max_output_tokens=4_096,
        safety_margin_tokens=4_096,
    )

    assert proof.fits is True
    assert proof.total_tokens == (
        proof.rendered_input_tokens
        + proof.constrained_max_output_tokens
        + proof.safety_margin_tokens
    )
    changed = prove_checkpoint_generation_fit(
        target_catalog_id="codex/gpt",
        proven_target_window_tokens=32_000,
        serialized_request='{"prompt":"changed"}',
        rendered_input_tokens=2_000,
        constrained_max_output_tokens=4_096,
        safety_margin_tokens=4_096,
    )
    assert changed.request_sha256 != proof.request_sha256


def test_a_checkpoint_generation_proof_uses_the_targets_whole_window():
    """A big model folds against its OWN window, not a 262K ceiling.

    This asserted the opposite until 2026-08-11: a fold on a 1M-token model was
    proved against 262,144 and declared not to fit at 260K of input — while the
    model had 740K to spare.
    """
    from core.destination_fit import prove_checkpoint_generation_fit

    proof = prove_checkpoint_generation_fit(
        target_catalog_id="native/large",
        proven_target_window_tokens=1_000_000,
        serialized_request="frozen",
        rendered_input_tokens=262_144 - 2_000,
        constrained_max_output_tokens=1_500,
        safety_margin_tokens=1_000,
    )

    assert proof.effective_window_tokens == 1_000_000
    assert proof.fits is True
    assert proof.block_reason is None


def test_live_model_switch_fit_does_not_manufacture_output_headroom_block(monkeypatch):
    from types import SimpleNamespace

    from thread.model_projection import _live_model_switch_fit

    monkeypatch.setattr(
        "llm.capabilities.get_model_max_output_tokens",
        lambda _model, fallback: 1_000,
    )
    target = SimpleNamespace(
        harness="claude-code",
        catalog_id="claude-code/sonnet",
        transport_model="sonnet",
        expected_reported_model=None,
        context_window=10_000,
    )
    result = _live_model_switch_fit(
        thread_id="switch-live-headroom",
        destination_target=target,
        live_context_usage=8_645,
        fit_input=_model_switch_fit_input(
            identity="model-switch:switch-live-headroom",
            content="",
        ),
    )

    assert result.status == "allowed"
    assert result.total_tokens == 8_645
    assert result.prepared_context.fit_ledger.rendered_input_tokens == result.total_tokens


def test_live_model_switch_fit_counts_queued_images(monkeypatch):
    from types import SimpleNamespace

    from core.media_projection import ValidatedImage, ValidatedMediaProjection
    from thread.model_projection import _live_model_switch_fit

    monkeypatch.setattr(
        "llm.capabilities.get_model_max_output_tokens",
        lambda _model, fallback: 1_000,
    )
    projection = ValidatedMediaProjection((
        ValidatedImage("image-1", "image/png", b"image", "a" * 64, 5),
    ), ("a" * 64,))
    result = _live_model_switch_fit(
        thread_id="switch-live-image",
        destination_target=SimpleNamespace(
            harness="codex",
            catalog_id="codex/image-model",
            transport_model="image-model",
            expected_reported_model=None,
            context_window=20_000,
        ),
        live_context_usage=100,
        fit_input=_model_switch_fit_input(
            identity="model-switch:switch-live-image",
            content="describe this",
            media_projection=projection,
        ),
    )

    assert result.prepared_context is not None
    assert result.prepared_context.fit_ledger is not None
    assert result.prepared_context.fit_ledger.rendered_input_tokens == 1_104
    assert dict(result.prepared_context.fit_ledger.section_tokens)[
        "current_input"
    ] == 1_004


@pytest.mark.asyncio
async def test_unledgered_custom_model_switch_attaches_once_at_full_window(
    monkeypatch,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from core import destination_preflight
    from core.context_payload import (
        PreparedContextPayload,
        RenderedDestinationPayload,
    )
    from core.destination_fit import UNPROVEN_WINDOW_FALLBACK_TOKENS
    from thread import model_projection as session_model

    target = SimpleNamespace(
        harness="codex",
        catalog_id="codex/large",
        context_window=1_000_000,
    )
    selection = SimpleNamespace(
        mode="exact",
        history_epoch=7,
        sealed_boundary=None,
        checkpoint_json=None,
        checkpoint_policy_version=None,
        checkpoint_generation_id=None,
        checkpoint_boundary=None,
    )
    rendered = RenderedDestinationPayload(
        payload={"wire": "frozen"},
        input_tokens=sum(SECTIONS.values()),
        accounting_payload={"wire": "frozen"},
        fit_section_tokens=SECTIONS,
        fit_input_identity="model-switch:thread-1",
        fit_current_input="",
        fit_components=_components(),
    )
    prepared = PreparedContextPayload(
        mode="exact_replay",
        target_catalog_id=target.catalog_id,
        model_context_window=target.context_window,
        history_epoch=selection.history_epoch,
        sealed_boundary=None,
        replay=selection,
        rendered=rendered,
    )
    attach = MagicMock(wraps=destination_preflight.attach_destination_fit_ledger)
    monkeypatch.setattr(destination_preflight, "attach_destination_fit_ledger", attach)
    monkeypatch.setattr(
        "llm.capabilities.get_model_max_output_tokens",
        lambda _model, fallback: 8_192,
    )

    result = await session_model._render_model_switch_projection(
        renderer=AsyncMock(return_value=prepared),
        thread_id="thread-1",
        source_model="codex/source",
        source_selected_model="codex/source",
        destination_target=target,
        selection=selection,
        fit_input=_model_switch_fit_input(
            identity="model-switch:thread-1",
            content="",
        ),
    )

    assert result.status == "allowed"
    # The target's proven window, uncapped (owner decision, 2026-08-11).
    assert result.model_context_window == 1_000_000
    attach.assert_called_once()


@pytest.mark.asyncio
async def test_preledgered_model_switch_preserves_exact_ledger_identity(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from core import destination_preflight
    from core.context_payload import PreparedContextPayload
    from core.destination_preflight import attach_destination_fit_ledger
    from thread import model_projection as session_model

    target = SimpleNamespace(
        harness="native",
        catalog_id="native/model",
        context_window=80_000,
    )
    selection = SimpleNamespace(
        mode="exact",
        history_epoch=7,
        sealed_boundary=None,
        checkpoint_json=None,
        checkpoint_policy_version=None,
        checkpoint_generation_id=None,
        checkpoint_boundary=None,
    )
    prepared = attach_destination_fit_ledger(
        PreparedContextPayload(
            mode="exact_replay",
            target_catalog_id=target.catalog_id,
            model_context_window=target.context_window,
            history_epoch=selection.history_epoch,
            sealed_boundary=None,
            replay=selection,
            rendered=_rendered_destination_payload(),
        ),
        harness=target.harness,
        proven_target_window_tokens=target.context_window,
    )
    renderer_ledger = prepared.fit_ledger
    attach = MagicMock(wraps=destination_preflight.attach_destination_fit_ledger)
    monkeypatch.setattr(destination_preflight, "attach_destination_fit_ledger", attach)

    result = await session_model._render_model_switch_projection(
        renderer=AsyncMock(return_value=prepared),
        thread_id="thread-1",
        source_model="native/source",
        source_selected_model="native/source",
        destination_target=target,
        selection=selection,
        fit_input=_model_switch_fit_input(
            identity="fifo-head",
            content="FIFO_HEAD",
        ),
    )

    attach.assert_not_called()
    assert result.prepared_context is prepared
    assert result.prepared_context.fit_ledger is renderer_ledger


def test_absent_ledger_is_distinct_from_a_mismatched_ledger() -> None:
    """An unattached ledger must not be reported as a provider condition.

    A dispatch path that never attaches a ledger is a caller defect. Raising
    the same error as a mismatched ledger makes the two indistinguishable at
    the call site and in operational triage.
    """
    from core.destination_fit import (
        ProviderCallBlocked,
        ProviderFitLedgerMissing,
        require_provider_call_fit,
    )

    with pytest.raises(ProviderFitLedgerMissing) as excinfo:
        require_provider_call_fit(
            None,
            harness="native",
            target_catalog_id="local/model",
            accounting_payload={"messages": [], "tools": []},
            input_identity="id-1",
            current_input="hello",
        )

    assert "no destination fit ledger" in str(excinfo.value)
    # Still fail-closed for every existing handler.
    assert isinstance(excinfo.value, ProviderCallBlocked)


def test_native_prepared_context_can_carry_a_fit_ledger() -> None:
    """The native dispatch payload must be able to hold the ledger at all."""
    from dataclasses import fields, replace

    from core.turn_context import NativePreparedContext

    assert "fit_ledger" in {f.name for f in fields(NativePreparedContext)}

    payload = NativePreparedContext(
        messages=(),
        context_usage={},
        tool_definitions=(),
        context_identity=None,
    )
    assert payload.fit_ledger is None
    assert replace(payload, fit_ledger="LEDGER").fit_ledger == "LEDGER"


def test_native_barrier_accepts_what_the_turn_actually_passes() -> None:
    """The turn must pass the barrier the value the ledger was built from.

    The native ledger digests the rendered FIFO head message. The turn body
    supplies the barrier's ``current_input``. If those are different values --
    the head message versus the raw user text -- the digests never match and
    the barrier rejects every native turn as a FIFO-input mismatch, before any
    provider is contacted.
    """
    from core.context_payload import (
        PreparedContextPayload,
        RenderedDestinationPayload,
    )
    from core.destination_preflight import attach_destination_fit_ledger
    from core.destination_fit import (
        ProviderCallBlocked,
        require_provider_call_fit,
    )
    from core.turn_context import NativePreparedContext
    from core.turn_iteration_context import current_fit_input

    messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "the current turn input"},
    ]
    tools: list[dict] = []
    prepared_native = NativePreparedContext(
        messages=tuple(messages),
        context_usage={"rendered_input_tokens": 10, "user_tokens": 10},
        tool_definitions=tuple(tools),
        context_identity=None,
    )

    # Built exactly as the native render path builds it.
    prepared = attach_destination_fit_ledger(
        PreparedContextPayload(
            mode="exact_replay",
            target_catalog_id="local/model",
            model_context_window=200_000,
            history_epoch=0,
            sealed_boundary=None,
            replay=None,
            rendered=RenderedDestinationPayload(
                payload=prepared_native,
                input_tokens=10,
                accounting_payload={
                    "messages": list(prepared_native.messages),
                    "tools": list(prepared_native.tool_definitions),
                },
                fit_section_tokens={
                    "fixed": 0,
                    "checkpoint": 0,
                    "tail": 0,
                    "current_input": 10,
                    "tools": 0,
                    "provider_wrapper": 0,
                },
                fit_input_identity="native-input-identity",
                fit_current_input=prepared_native.messages[-1],
                fit_components={"payload": "native"},
            ),
        ),
        harness="native",
        proven_target_window_tokens=200_000,
    )

    ledger = prepared.fit_ledger
    # Exactly what the turn body derives and hands to the barrier.
    current_input = current_fit_input(messages)

    accepted = require_provider_call_fit(
        ledger,
        harness="native",
        target_catalog_id="local/model",
        accounting_payload={"messages": messages, "tools": tools},
        input_identity=ledger.input_identity,
        current_input=current_input,
    )
    assert accepted is ledger

    # The raw user text is a different value and must not satisfy the
    # barrier; this is what made every native turn fail.
    with pytest.raises(ProviderCallBlocked, match="current FIFO input"):
        require_provider_call_fit(
            ledger,
            harness="native",
            target_catalog_id="local/model",
            accounting_payload={"messages": messages, "tools": tools},
            input_identity=ledger.input_identity,
            current_input="the current turn input",
        )
