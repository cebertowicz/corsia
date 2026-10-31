"""A prompt the Mac cannot prefill goes back to the boundary once, then ends handled.

Covers the Turn half of the local-overflow recovery: the loop's one retry after a
provider size refusal, the futile optional fold, and the memory-window copy.
"""

from __future__ import annotations

import asyncio
import pathlib
from types import SimpleNamespace

import pytest

from core import turn_iteration
from core import turn_iteration_context as iteration_context
from core.checkpoint_resume import CheckpointWaitScope, ProviderBoundary, maintain_before_provider_call
from core.checkpoint_types import CHAT_TOO_LONG_FOR_MEMORY_MESSAGE, CheckpointUnavailable
from core.fork_fit import ColdFoldInputTooLarge, ProviderPayload, evaluate_boundary
from core.turn_core import TurnIdentity
from core.turn_provider_error import TurnProviderError
from llm.router import LLMResponse

OVERFLOW = LLMResponse(
    "prefill memory guard rejected", finish_reason="error", error_kind="provider_context_overflow",
)
ANSWER = LLMResponse("done")


async def _run(monkeypatch, responses, *, windows):
    """Run the loop with scripted provider responses and one window per boundary."""
    frames: list[str] = []
    boundaries: list[int] = []
    calls = iter(responses)
    ledger = SimpleNamespace(effective_window_tokens=windows[0])
    state = turn_iteration._IterationState(
        messages=[{"role": "user", "content": "go"}], context_usage={}, tool_definitions=[],
        context_identity=None, fit_ledger=ledger, fit_current_input="go",
        checkpoint_source_messages=[], turn_model="local/m",
    )
    window_at_boundary = iter(windows)

    async def maintain(**_kwargs):
        window = next(window_at_boundary)
        boundaries.append(window)
        state.fit_ledger = SimpleNamespace(effective_window_tokens=window)

    async def call_provider(*_args, **_kwargs):
        return next(calls), False

    async def on_step(kind, _payload):
        frames.append(kind)

    monkeypatch.setattr(iteration_context, "maintain_native_checkpoint", maintain)
    monkeypatch.setattr(turn_iteration, "_call_provider", call_provider)
    agent = SimpleNamespace(max_iterations=6, _strip_think=lambda text: text)
    outcome = None
    try:
        await turn_iteration._run_loop(
            agent, state, "t", "local/m", {}, asyncio.Event(), on_step, None, None, None, None, None,
        )
    except TurnProviderError as error:
        outcome = error
    return frames, boundaries, outcome, state


@pytest.mark.asyncio
async def test_overflow_once_checkpoints_at_the_smaller_window_then_completes(monkeypatch):
    frames, boundaries, error, state = await _run(
        monkeypatch, [OVERFLOW, ANSWER], windows=[40_000, 28_672],
    )

    assert error is None
    assert boundaries == [40_000, 28_672]
    assert "error" not in frames and frames[-1] == "turn_complete"
    assert state.iteration == 2


@pytest.mark.asyncio
async def test_overflow_twice_in_one_turn_ends_as_before(monkeypatch):
    frames, boundaries, error, _state = await _run(
        monkeypatch, [OVERFLOW, OVERFLOW, ANSWER], windows=[40_000, 28_672],
    )

    assert isinstance(error, TurnProviderError)
    assert "Retry the send" in str(error)
    assert frames.count("error") == 1
    assert boundaries == [40_000, 28_672]


@pytest.mark.asyncio
async def test_a_window_that_did_not_shrink_ends_handled_without_a_second_call(monkeypatch):
    frames, boundaries, error, state = await _run(
        monkeypatch, [OVERFLOW, ANSWER], windows=[28_672, 28_672],
    )

    assert isinstance(error, TurnProviderError)
    assert frames == ["error"]
    assert state.iteration == 1  # the retry never reached the provider


@pytest.mark.asyncio
async def test_a_later_unrelated_error_after_the_retry_still_ends_the_turn(monkeypatch):
    generic = LLMResponse("down", finish_reason="error", error_kind="provider_server_unreachable")
    frames, _boundaries, error, _state = await _run(
        monkeypatch, [OVERFLOW, generic], windows=[40_000, 28_672],
    )

    assert str(error) == "down"
    assert frames == ["error"]


def _payload(**values) -> ProviderPayload:
    base = dict(
        context_window=28_672, canonical_input_tokens=21_000, input_tokens=None, minimum_tokens=2_000,
        checkpoint_status=None, installed_input_tokens=None, checkpoint_admissible=True,
    )
    return ProviderPayload(**{**base, **values})


def test_a_fold_that_cannot_leave_the_launch_point_is_not_started_optionally():
    # Launch point is 28,672 - 8,192 = 20,480 tokens.
    assert evaluate_boundary(_payload(minimum_tokens=21_000)).value == "proceed"


def test_a_normal_window_still_folds_at_the_launch_point():
    assert evaluate_boundary(_payload(minimum_tokens=2_000)).value == "proceed_and_fold"
    big = _payload(context_window=262_144, canonical_input_tokens=237_000, minimum_tokens=8_000)
    assert evaluate_boundary(big).value == "proceed_and_fold"


def _boundary(**values) -> ProviderBoundary:
    async def render(record):  # pragma: no cover - never reached
        raise AssertionError(record)

    base = dict(
        thread_id="t", turn=TurnIdentity("turn", 1), context_window=28_672,
        canonical_input_tokens=30_000, provider_input_tokens=None, minimum_input_tokens=2_000,
        adopted_checkpoint_id=None, active_checkpoint=None, installed_checkpoint=None,
        prepare_checkpoint=None, render_installed=render, wait_scope=CheckpointWaitScope(),
    )
    return ProviderBoundary(**{**base, **values})


async def _refused(**values) -> CheckpointUnavailable:
    with pytest.raises(CheckpointUnavailable) as raised:
        await maintain_before_provider_call(_boundary(**values))
    return raised.value


async def _too_large(_checkpoint_id, _required):
    raise ColdFoldInputTooLarge("minimum meaningful cold fork input does not fit")


@pytest.mark.asyncio
@pytest.mark.parametrize("values", [
    {},  # no checkpoint is admissible
    {"minimum_input_tokens": 40_000, "prepare_checkpoint": _too_large},  # the floor exceeds the window
    {"prepare_checkpoint": _too_large},  # the fold's generation cannot fit
])
async def test_memory_window_that_nothing_fits_tells_the_person_what_to_do(values):
    error = await _refused(window_source="memory", **values)

    assert str(error) == CHAT_TOO_LONG_FOR_MEMORY_MESSAGE
    assert error.context_window == 28_672  # the settlement's stable-window comparison


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["provider", "user", None])
async def test_other_window_sources_keep_the_mechanism_text(source):
    error = await _refused(window_source=source, minimum_input_tokens=40_000)

    assert str(error) == "the minimum provider continuation exceeds the context window"


def test_the_frontend_label_mirrors_the_backend_sentence():
    root = pathlib.Path(__file__).resolve().parents[2] / "frontend/src/lib/messageDeliveryCopy.ts"

    assert CHAT_TOO_LONG_FOR_MEMORY_MESSAGE in root.read_text()
