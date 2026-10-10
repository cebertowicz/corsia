from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from shared_send_fixtures import _insert_session, _thread_row


@pytest.mark.parametrize(
    ("harness", "model"),
    [
        ("claude-code", "claude-code/sonnet"),
        ("codex", "codex/gpt-5-codex"),
    ],
)
@pytest.mark.asyncio
async def test_external_agent_send_keeps_raw_user_and_provider_framing(
    isolated_db,
    monkeypatch,
    harness,
    model,
):
    from core.message_history import render_agent_envelope
    from core.token_estimation import measure_text
    from infra.context_usage_attribution import merge_context_usage_with_snapshot
    from infra.send_service import send_thread_input
    from subagents import cc_runtime, codex_runtime

    thread_id = f"{harness}-agent-terminal"
    raw_user = "inspect the exact terminal ledger"
    metadata = {
        "sender_kind": "agent",
        "from_thread": "sender-thread",
        "from_title": "Planner",
    }
    _insert_session(thread_id, model=model, spawned_by=None)
    envelope = render_agent_envelope(raw_user, metadata)

    async def accept_cc(*_args, **kwargs):
        await kwargs["settle_delivery"]()
        return kwargs["preadmitted_turn"]

    async def accept_codex(*_args, **kwargs):
        await kwargs["bind_input_turn"](kwargs["successor_turn_id"], None)
        await kwargs["settle_delivery"]()
        return kwargs["preadmitted_turn"]

    if harness == "claude-code":
        feed_main = AsyncMock(side_effect=accept_cc)
        monkeypatch.setattr(cc_runtime, "feed_main", feed_main)
        monkeypatch.setattr(cc_runtime, "is_live", lambda _sid: False)
    else:
        feed_main = AsyncMock(side_effect=accept_codex)
        monkeypatch.setattr(
            codex_runtime, "ensure_connection_available", lambda db=None: None,
        )
        monkeypatch.setattr(
            codex_runtime, "has_thread_id", AsyncMock(return_value=False),
        )
        monkeypatch.setattr(
            codex_runtime, "has_prior_messages", AsyncMock(return_value=False),
        )
        monkeypatch.setattr(codex_runtime, "feed_main", feed_main)

    await send_thread_input(
        thread_id,
        _thread_row(thread_id),
        raw_user,
        metadata=metadata,
        native_mode="delegation",
    )

    provider_frame = feed_main.await_args.args[1]
    assert provider_frame.startswith(
        "CORSIA is delivering 2 queued inbound inputs together."
    )
    assert "[1/2]\n[PASSIVE CONTEXT DIGEST]" in provider_frame
    assert provider_frame.endswith(f"[2/2]\n{envelope.content}")
    destination = feed_main.await_args.kwargs["prepared_context"].rendered.payload
    if harness == "claude-code":
        reconstruction = destination.frame_reconstruction
        provider_key = "cc_user_frame_text"
    else:
        reconstruction = destination.reconstruction
        provider_key = "codex_user_text"

    snapshot = reconstruction.snapshot.to_dict()
    material = snapshot["frozen_prompt_material"]
    assert material["current_user_content"].endswith(f"[2/2]\n{raw_user}")
    assert "[PASSIVE CONTEXT DIGEST]" in material["current_user_content"]
    assert material["provider_payload"][provider_key] == provider_frame
    framing_source = next(
        source for source in snapshot["source_inventory"]
        if source["source_id"] == f"{harness}:agent-send-envelope"
    )
    framing_tokens = (
        measure_text(envelope.content, model=model).tokens
        - measure_text(envelope.body_content, model=model).tokens
    )
    assert framing_source["origin_section"] == "provider"
    assert framing_source["prompt_tokens"] == framing_tokens

    terminal = merge_context_usage_with_snapshot(
        {"model_context_window": 200_000, "total_used": 1_000},
        snapshot=snapshot,
    )
    assert terminal["user_tokens"] == measure_text(
        material["current_user_content"], model=model,
    ).tokens
    assert terminal["context_source_tokens"] >= framing_tokens
