"""A warm fork is admitted only when the checkpoint still has room to write."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import Mock

import pytest
from core.checkpoint_fork_kind import CHECKPOINT_KIND

from core.fork_types import (
    ProviderForkRejected,
    ProviderForkResult,
    WarmForkUnusable,
)
from core.fork_types import (
    FORK_OUTPUT_TOKENS,
    FORK_TOOL_ROUND_TRIP_TOKENS,
)
from test_checkpoint_warm_fork_unusable import _cold_admission, _warm_request

WINDOW = 262_144
RESERVE = FORK_OUTPUT_TOKENS + FORK_TOOL_ROUND_TRIP_TOKENS
INSTRUCTION = 5  # patched below so the arithmetic is exact


def _patch_cc(monkeypatch, loaded: int) -> None:
    from core import fork_cc_cold as cold

    async def loaded_context(_process, _request, _spawned_at):
        return loaded, WINDOW

    monkeypatch.setattr(cold, "_loaded_context", loaded_context)
    monkeypatch.setattr(
        "core.token_estimation.measure_request",
        lambda *_a, **_k: Mock(tokens=INSTRUCTION),
    )


@pytest.mark.asyncio
async def test_cc_warm_fork_without_output_room_is_unusable(monkeypatch):
    from core.fork_cc_cold import prepare_warm_payload

    _patch_cc(monkeypatch, WINDOW - INSTRUCTION - RESERVE + 1)
    request = _warm_request("claude-code")
    assert await prepare_warm_payload(None, request, asyncio.Event(), 0.0) is None


@pytest.mark.asyncio
async def test_cc_warm_fork_that_just_fits_stays_warm(monkeypatch):
    from core.fork_cc_cold import prepare_warm_payload

    _patch_cc(monkeypatch, WINDOW - INSTRUCTION - RESERVE)
    request = _warm_request("claude-code")
    assert await prepare_warm_payload(None, request, asyncio.Event(), 0.0)


@pytest.mark.asyncio
async def test_cc_warm_fork_without_output_room_corrects_to_cold(monkeypatch):
    from core import fork_cc_io as cc_io
    from core import checkpoint_cold_fallback as fallback

    async def fake_run_in_db(*_args, **_kwargs):
        return None

    monkeypatch.setattr(fallback, "run_in_db", fake_run_in_db)
    _patch_cc(monkeypatch, WINDOW - INSTRUCTION - RESERVE + 1)
    calls = []

    async def invoke(request, cancel_event):
        calls.append(request)
        if request.warm_source is not None:
            await cc_io._exchange(None, request, cancel_event, None, None, [], [], 0.0)
        return ProviderForkResult("cold body", None)

    target = _warm_request("claude-code").target
    result = await fallback.invoke_with_cold_correction(
        _warm_request(
            "claude-code", cold_builder=lambda: _cold_admission(target=target),
        ),
        asyncio.Event(), invoke,
    )
    assert result.raw_body == "cold body"
    assert [c.warm_source is not None for c in calls] == [True, False]


async def _codex_initial(monkeypatch, tmp_path, *, warm: bool, tokens: int):
    from core import fork_codex as codex
    from test_checkpoint_external_adapters import _request

    monkeypatch.setattr(
        "core.fork_codex_context.frozen_context_tokens", lambda *_a, **_k: tokens,
    )
    monkeypatch.setattr(
        "core.token_estimation.measure_request", lambda *_a, **_k: Mock(tokens=0),
    )
    session = Mock(side_effect=RuntimeError("session started"))
    monkeypatch.setattr(codex, "_fork_session", session)
    target = replace(_warm_request("codex").target, context_window=WINDOW)
    source = Mock(rollout_path=str(tmp_path / "r.jsonl"), rollout_bytes=0) if warm else None
    request = _request(target, source, binary="unused")
    cold_input = None if warm else Mock(input_tokens=tokens)
    await codex._run_app_server_fork(
        request, source, source_home=tmp_path, isolated_home=tmp_path, cwd=str(tmp_path),
        cancel_event=asyncio.Event(), cold_input=cold_input, kind=CHECKPOINT_KIND,
    )


@pytest.mark.asyncio
async def test_codex_warm_fork_without_output_room_is_unusable(monkeypatch, tmp_path):
    with pytest.raises(WarmForkUnusable):
        await _codex_initial(monkeypatch, tmp_path, warm=True, tokens=WINDOW - RESERVE + 1)


@pytest.mark.asyncio
async def test_codex_warm_fork_that_just_fits_starts_a_session(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="session started"):
        await _codex_initial(monkeypatch, tmp_path, warm=True, tokens=WINDOW - RESERVE)


@pytest.mark.asyncio
async def test_codex_cold_overflow_stays_a_plain_rejection(monkeypatch, tmp_path):
    with pytest.raises(ProviderForkRejected) as caught:
        await _codex_initial(monkeypatch, tmp_path, warm=False, tokens=WINDOW + 1)
    assert not isinstance(caught.value, WarmForkUnusable)
