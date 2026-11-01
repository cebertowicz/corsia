"""A prompt the Mac cannot prefill goes back to the boundary once, then ends handled.

Covers the Turn half of the local-overflow recovery: the loop's one retry after a
provider size refusal, and the futile optional fold with its system-prompt floor.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core import turn_iteration
from core import turn_iteration_context as iteration_context
from core.checkpoint_resume import CheckpointWaitScope
from core.fork_fit import BoundaryDecision, ProviderPayload, evaluate_boundary
from core.turn_core import TurnIdentity
from core.turn_provider_error import TurnProviderError
from llm.router import LLMResponse

OVERFLOW = LLMResponse(
    "prefill memory guard rejected", finish_reason="error", error_kind="provider_context_overflow",
)
ANSWER = LLMResponse("done")
CHECKPOINTED = object()


async def _run(monkeypatch, responses, *, windows, max_iterations=6, checkpoints=True, adopted=None):
    """Run the loop with scripted provider responses and one window per boundary.

    ``checkpoints`` is whether the retry boundary adopts a checkpoint; ``adopted``
    collects what the loop adopted.
    """
    frames: list[str] = []
    boundaries: list[int] = []
    adopted = [] if adopted is None else adopted
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
        return CHECKPOINTED if state.overflow_retry_pending and checkpoints else None

    async def call_provider(*_args, **_kwargs):
        return next(calls), False

    async def on_step(kind, _payload):
        frames.append(kind)

    monkeypatch.setattr(iteration_context, "maintain_native_checkpoint", maintain)
    monkeypatch.setattr(iteration_context, "adopt_native_context", lambda _state, prepared: adopted.append(prepared))
    monkeypatch.setattr(turn_iteration, "_call_provider", call_provider)
    agent = SimpleNamespace(max_iterations=max_iterations, _strip_think=lambda text: text)
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
    adopted: list[object] = []
    frames, boundaries, error, state = await _run(
        monkeypatch, [OVERFLOW, ANSWER], windows=[40_000, 28_672], adopted=adopted,
    )

    assert error is None
    assert boundaries == [40_000, 28_672]
    assert adopted == [CHECKPOINTED]  # the retry carries a checkpoint, not the refused payload
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
async def test_a_retry_boundary_that_adopts_no_checkpoint_ends_handled_without_a_second_call(monkeypatch):
    frames, boundaries, error, state = await _run(
        monkeypatch, [OVERFLOW, ANSWER], windows=[40_000, 28_672], checkpoints=False,
    )

    assert isinstance(error, TurnProviderError)
    assert frames == ["error"]
    assert state.iteration == 1  # the retry never reached the provider


@pytest.mark.asyncio
async def test_an_overflow_on_the_last_iteration_ends_as_an_error_not_a_finished_turn(monkeypatch):
    frames, boundaries, error, state = await _run(
        monkeypatch, [OVERFLOW, ANSWER], windows=[40_000, 28_672], max_iterations=1,
    )

    assert isinstance(error, TurnProviderError)
    assert "Retry the send" in str(error)
    assert frames == ["error"]  # no turn_complete, no maximum-iterations text
    assert boundaries == [40_000]  # no retry boundary was granted
    assert state.iteration == 1 and not state.overflow_retry_pending


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


def _usage(system: int, tools: int = 0) -> dict:
    return {"fit_section_tokens": {"fixed": system, "tools": tools}}


def _sections(fixed: int, tools: int, current: int) -> tuple[tuple[str, int], ...]:
    return (("fixed", fixed), ("current_input", current), ("tools", tools), ("provider_wrapper", 0))


async def _folds_at_boundary(monkeypatch, *, window, rendered, usage, sections) -> bool:
    """Run the real Native boundary and report whether it started an optional fold."""
    from core import checkpoint_native_admission

    started: list[str] = []

    def snapshot(*_args, **_kwargs):
        return None, None, None

    async def run_in_db(function, *_args):
        if function is snapshot:
            return None, None, None
        return SimpleNamespace(context_window=window, context_window_source="provider")

    async def prepare(checkpoint_id, *_args, **_kwargs):
        started.append(checkpoint_id)

    monkeypatch.setattr(iteration_context, "provider_boundary_snapshot", snapshot)
    monkeypatch.setattr(iteration_context, "run_in_db", run_in_db)
    monkeypatch.setattr(iteration_context, "partial", lambda function, *_a, **_k: function)
    monkeypatch.setattr(checkpoint_native_admission, "skip_optional_cold_fold", lambda *_a: None)
    monkeypatch.setattr(checkpoint_native_admission, "prepare_native_checkpoint", prepare)
    monkeypatch.setattr(iteration_context, "_read_native_checkpoint_context", lambda *_a: object())
    ledger = SimpleNamespace(
        target_catalog_id="local/m", effective_window_tokens=window, rendered_input_tokens=rendered,
        input_identity="i", section_tokens=sections, fits=True,
    )
    state = turn_iteration._IterationState(
        messages=[{"role": "user", "content": "go"}], context_usage=usage, tool_definitions=[],
        context_identity=None, fit_ledger=ledger, fit_current_input="go",
        checkpoint_source_messages=[], turn_model="local/m",
    )
    cancel = SimpleNamespace(checkpoint_wait_scope=CheckpointWaitScope())
    await iteration_context.maintain_native_checkpoint(
        agent=None, state=state, thread_id="t", turn_identity=TurnIdentity("turn", 1),
        cancel_event=cancel, turn_model="local/m", reasoning_effort=None, project_id=None,
    )
    return bool(started)


@pytest.mark.asyncio
@pytest.mark.parametrize("window,system,tools,current,rendered,folds", [
    # 32 GB seat: 18,618 fixed split 14,000 system prompt + 4,618 tools, 2,000 current input.
    # Launch point 20,480; the smallest continuation is 20,618, so no optional fold starts.
    (28_672, 14_000, 4_618, 2_000, 21_000, False),
    # System prompt and tools already at the launch point with a small current input.
    (58_000, 40_000, 10_000, 500, 52_000, False),
    # A normal window still folds at its launch point (233,472).
    (262_144, 8_000, 4_000, 500, 237_000, True),
])
async def test_the_futile_fold_minimum_counts_the_system_prompt(
    monkeypatch, window, system, tools, current, rendered, folds,
):
    started = await _folds_at_boundary(
        monkeypatch, window=window, rendered=rendered, usage=_usage(system),
        sections=_sections(rendered - tools - current, tools, current),
    )

    assert started is folds


@pytest.mark.asyncio
async def test_the_system_measure_ignores_history_a_tool_phase_refit_absorbed(monkeypatch):
    from core.destination_preflight import attach_native_provider_fit_ledger

    system = {"role": "system", "content": "s " * 600}
    history = [{"role": "user" if i % 2 else "assistant", "content": "h " * 400} for i in range(170)]
    frame = {"role": "user", "content": "go"}
    ledger = attach_native_provider_fit_ledger(
        target_catalog_id="local/m", proven_target_window_tokens=58_000,
        messages=[system, *history, frame], tools=[], input_identity="i", current_input=frame,
    ).fit_ledger
    absorbed = dict(ledger.section_tokens)["fixed"]
    assert absorbed > 43_500  # read as a system measure, this would suppress the fold

    started = await _folds_at_boundary(
        monkeypatch, window=58_000, rendered=ledger.rendered_input_tokens,
        usage=_usage(1_200), sections=ledger.section_tokens,
    )

    assert started is True


def _fork_request(window: int, carried: int):
    from core.fork_types import ProviderForkRequest
    from llm.resolution import ExecutionTarget

    target = ExecutionTarget(
        catalog_id="local/m", harness="native", provider="omlx", backend="local",
        transport_model="m", expected_reported_model=None, context_window=window,
        available=True, availability_reason=None, display_name="m",
        supports_reasoning=True, preserve_thinking_default=True,
    )
    return ProviderForkRequest(
        fork_id="f", thread_id="t", target=target, instruction="write",
        reasoning_effort=None, working_dir="/w", carried_tokens=carried,
    )


@pytest.mark.parametrize("window,carried,allowance", [
    (48_000, 18_600, 8_700),  # was 9,600: launch 36,000, half of 17,400
    (36_000, 18_600, 4_200),  # launch 27,000, half of 8,400
    (262_144, 18_600, 16_384),  # a large window is unchanged
    (48_000, 0, 9_600),  # no measurement: the existing allowance
    (20_000, 18_600, 4_000),  # no half-room to take: window // 5, unchanged
    (48_000, 35_999, 9_600),  # launch - system is exactly 1: unchanged
])
def test_the_fork_output_allowance_is_half_the_room_above_the_carried_prompt(window, carried, allowance):
    from core.fork_native import _window_output

    assert _window_output(_fork_request(window, carried)).max_output_tokens == allowance


def test_a_full_checkpoint_body_leaves_the_chat_below_its_launch_point():
    from core.fork_native import _window_output
    from core.fork_pressure import soft_pressure_remaining

    window, carried, current = 48_000, 18_600, 1_000
    body = _window_output(_fork_request(window, carried)).max_output_tokens
    launch = window - soft_pressure_remaining(window)

    assert carried + body + current < launch
    assert launch - (carried + body + current) >= 7_000  # room left for turns


@pytest.mark.asyncio
async def test_the_fixed_prompt_and_tools_reach_the_warm_and_cold_fork_requests(monkeypatch):
    from core import checkpoint_native_admission as admission
    from history.types import HistoryBoundary
    from infra import database

    request = _fork_request(48_000, 0)
    context = SimpleNamespace(
        target=request.target, boundary=HistoryBoundary("t", 1, 1, "m1"),
        previous_generation_id=None, previous=None, projection=SimpleNamespace(),
        working_dir="/w",
    )
    # The 32 GB seat: 14,000 fixed prompt + 4,618 tool definitions.
    state = SimpleNamespace(context_usage=_usage(14_000, 4_618))

    async def source(*_args):
        return object()

    monkeypatch.setattr(admission, "capture_native_checkpoint_source", source)
    monkeypatch.setattr(database, "get_db", lambda: SimpleNamespace(close=lambda: None))
    warm = await admission.prepare_native_checkpoint(
        "c1", "t", state, context, SimpleNamespace(turn_id="x"), None,
    )
    cold = await warm.provider_request.same_model_cold()

    assert warm.provider_request.carried_tokens == 18_618
    assert cold.provider_request.carried_tokens == 18_618


async def _boundary_call(monkeypatch, *, retry, installed_id, adopted_id="ck-1"):
    """Run the real Native boundary and capture what it asks of the checkpoint layer."""
    from core import checkpoint_resume
    from core.checkpoint_resume import ProviderContinuation

    seen: dict = {}
    installed = SimpleNamespace(id=installed_id)

    async def run_in_db(function, *_args):
        if function is iteration_context.provider_boundary_snapshot:
            return None, installed, None
        return SimpleNamespace(context_window=28_672, context_window_source="provider")

    async def maintain(boundary, *, must_adopt=False):
        seen.update(must_adopt=must_adopt, installed=boundary.installed_checkpoint)
        return ProviderContinuation(BoundaryDecision.PROCEED, boundary.turn)

    monkeypatch.setattr(iteration_context, "run_in_db", run_in_db)
    monkeypatch.setattr(checkpoint_resume, "maintain_before_provider_call", maintain)
    ledger = SimpleNamespace(
        target_catalog_id="local/m", effective_window_tokens=28_672, rendered_input_tokens=21_000,
        input_identity="i", section_tokens=_sections(14_000, 4_618, 2_000), fits=True,
    )
    state = turn_iteration._IterationState(
        messages=[{"role": "user", "content": "go"}],
        context_usage={"checkpoint_generation_id": adopted_id, **_usage(14_000, 4_618)},
        tool_definitions=[], context_identity=None, fit_ledger=ledger, fit_current_input="go",
        checkpoint_source_messages=[], turn_model="local/m",
    )
    state.overflow_retry_pending = retry
    cancel = SimpleNamespace(checkpoint_wait_scope=CheckpointWaitScope())
    await iteration_context.maintain_native_checkpoint(
        agent=None, state=state, thread_id="t", turn_identity=TurnIdentity("turn", 1),
        cancel_event=cancel, turn_model="local/m", reasoning_effort=None, project_id=None,
    )
    return seen, installed


@pytest.mark.asyncio
async def test_the_overflow_retry_boundary_must_adopt_a_checkpoint_newer_than_the_refused_one(monkeypatch):
    seen, _installed = await _boundary_call(monkeypatch, retry=True, installed_id="ck-1")

    assert seen == {"must_adopt": True, "installed": None}


@pytest.mark.asyncio
async def test_the_overflow_retry_may_adopt_a_checkpoint_installed_since_the_refusal(monkeypatch):
    seen, installed = await _boundary_call(monkeypatch, retry=True, installed_id="ck-2")

    assert seen == {"must_adopt": True, "installed": installed}


@pytest.mark.asyncio
async def test_an_ordinary_boundary_does_not_force_a_checkpoint(monkeypatch):
    seen, installed = await _boundary_call(monkeypatch, retry=False, installed_id="ck-1")

    assert seen == {"must_adopt": False, "installed": installed}
