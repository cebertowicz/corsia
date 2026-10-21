"""A warm-fork correction names the refusal's real cause and keeps an optional review out of the fit."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import Mock

import pytest
from loguru import logger

from core.fork_types import (
    ProviderForkResult,
    WarmForkUnusable,
)
from core.checkpoint_types import (
    CHECKPOINT_OUTPUT_TOKENS,
    CHECKPOINT_TOOL_ROUND_TRIP_TOKENS,
)
from test_checkpoint_warm_fork_unusable import _cold_admission, _warm_request

WINDOW = 262_144
RESERVE = CHECKPOINT_OUTPUT_TOKENS + CHECKPOINT_TOOL_ROUND_TRIP_TOKENS
BARE_TOKENS, REVIEWED_TOKENS = 100, 8_000


async def _refusal_lines(monkeypatch, refusal: WarmForkUnusable) -> list[str]:
    from core import checkpoint_cold_fallback as fallback

    async def no_db(*_args, **_kwargs):
        return None

    async def invoke(request, _cancel):
        if request.warm_source is not None:
            raise refusal
        return ProviderForkResult("cold body", None)

    monkeypatch.setattr(fallback, "run_in_db", no_db)
    lines: list[str] = []
    sink = logger.add(lines.append, format="{message}", level="INFO")
    try:
        await fallback.invoke_with_cold_correction(
            _warm_request("native", cold_builder=_cold_admission),
            asyncio.Event(), invoke,
        )
    finally:
        logger.remove(sink)
    return lines


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["warm_fork_home_mismatch", "warm_fork_login_mismatch"])
async def test_refusal_line_names_the_account_mismatch_not_over_window(monkeypatch, reason):
    from connections.account_operation import _refuse_warm_source

    with pytest.raises(WarmForkUnusable) as caught:
        _refuse_warm_source(reason)

    assert await _refusal_lines(monkeypatch, caught.value) == [
        f"checkpoint warm capture refused for thread-1 (native): {reason}\n",
    ]


@pytest.mark.asyncio
async def test_refusal_line_defaults_to_over_window_for_the_adapters_own_refusal(monkeypatch):
    refusal = WarmForkUnusable("Claude checkpoint initial input exceeds selected context window")

    assert await _refusal_lines(monkeypatch, refusal) == [
        "checkpoint warm capture refused for thread-1 (native): warm_fork_over_window\n",
    ]


def _patch_fork(monkeypatch, loaded: int) -> None:
    from core import checkpoint_cc_cold as cold

    async def loaded_context(_process, _request, _spawned_at):
        return loaded, WINDOW

    def measure(payload, **_kwargs):
        text = payload["messages"][0]["content"]
        return Mock(tokens=REVIEWED_TOKENS if "review block" in text else BARE_TOKENS)

    monkeypatch.setattr(cold, "_loaded_context", loaded_context)
    monkeypatch.setattr("core.token_estimation.measure_request", measure)


def _reviewed_request():
    request = _warm_request("claude-code")
    return replace(
        request,
        instruction="checkpoint\n\nreview block",
        instruction_without_review="checkpoint",
    )


@pytest.mark.asyncio
async def test_warm_fork_that_cannot_hold_the_review_block_runs_without_it(monkeypatch):
    """The fork fits its bare instruction: the review must not turn that into a cold fold."""
    from core.checkpoint_cc_cold import prepare_warm_payload

    # Room for the bare instruction and the reserve, but not for the review block.
    _patch_fork(monkeypatch, WINDOW - BARE_TOKENS - RESERVE)
    lines: list[str] = []
    sink = logger.add(lines.append, format="{message}", level="INFO")
    try:
        payload = await prepare_warm_payload(None, _reviewed_request(), asyncio.Event(), 0.0)
    finally:
        logger.remove(sink)

    assert payload is not None
    assert b"review block" not in payload and b"checkpoint" in payload
    assert any("reason=no-room-at-fork" in line for line in lines)


@pytest.mark.asyncio
async def test_warm_fork_with_room_for_the_review_block_keeps_it(monkeypatch):
    from core.checkpoint_cc_cold import prepare_warm_payload

    _patch_fork(monkeypatch, WINDOW - REVIEWED_TOKENS - RESERVE)

    payload = await prepare_warm_payload(None, _reviewed_request(), asyncio.Event(), 0.0)

    assert payload is not None and b"review block" in payload


@pytest.mark.asyncio
async def test_warm_fork_that_cannot_hold_even_the_bare_instruction_stays_unusable(monkeypatch):
    """The fit check is not loosened: no room for the bare instruction still refuses."""
    from core.checkpoint_cc_cold import prepare_warm_payload

    _patch_fork(monkeypatch, WINDOW - BARE_TOKENS - RESERVE + 1)

    assert await prepare_warm_payload(None, _reviewed_request(), asyncio.Event(), 0.0) is None


@pytest.mark.asyncio
async def test_attach_review_remembers_the_instruction_it_extended(monkeypatch):
    from core import placement_review

    monkeypatch.setattr(placement_review, "review_block", lambda *_a, **_k: "review block")

    async def run_in_db(function, *args, **_kwargs):
        return function(*args)

    monkeypatch.setattr("infra.db_async.run_in_db", run_in_db)
    request = _warm_request("claude-code")

    reviewed = await placement_review.attach_review(request)

    assert reviewed.instruction == "checkpoint\n\nreview block"
    assert reviewed.instruction_without_review == "checkpoint"
    assert request.instruction_without_review is None
