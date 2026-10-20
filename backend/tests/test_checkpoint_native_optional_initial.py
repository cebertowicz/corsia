"""An optional Native fold without a correlated receipt waits for a warm one.

Before the first provider call, or after a call that reported no usage, no
correlated receipt exists, so an optional fold there could only go cold. It is
skipped at info level, apart from real admission failures, which still warn;
a later boundary folds warm from the next measured call. A required fold still
runs cold.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from history.types import HistoryBoundary

THREAD = "native-optional-thread"
WINDOW = 100_000


@dataclass(frozen=True)
class _Request:
    id: str
    thread_id: str
    originating_turn_id: str
    mode: str


@dataclass(frozen=True)
class _ProviderRequest:
    same_model_cold: object = None


@dataclass(frozen=True)
class _Admission:
    request: _Request
    provider_request: _ProviderRequest = _ProviderRequest()


class _Started(Exception):
    """Ends a required boundary once its fold has been requested."""


def _ledger(rendered: int):
    return SimpleNamespace(
        target_catalog_id="native/test",
        effective_window_tokens=WINDOW,
        rendered_input_tokens=rendered,
        total_tokens=rendered,
        input_identity="input-1",
        section_tokens=(("fixed", rendered), ("current_input", 0), ("tools", 0)),
        fits=True,
    )


def _state(rendered: int):
    from core.turn_iteration import _IterationState

    frame = {"role": "user", "content": "continue"}
    return _IterationState(
        messages=[frame], context_usage={}, tool_definitions=[],
        context_identity=None, fit_ledger=_ledger(rendered),
        fit_current_input=frame, checkpoint_source_messages=[],
        turn_model="native/test",
    )


@pytest.fixture
def folds(monkeypatch):
    """Record every fold request by mode through the real Native admission."""
    from core import checkpoint_native_admission, turn_iteration_context

    from llm import resolution

    requested: list[str] = []
    context = SimpleNamespace(
        boundary=HistoryBoundary(THREAD, 0, 3, "result-1"),
        target=SimpleNamespace(preserve_thinking_default=None),
        previous_generation_id=None,
        working_dir="/tmp",
    )

    def boundary_snapshot(*_args, **_kwargs):
        return None, None, None

    async def iteration_db(function, *args):
        if getattr(function, "func", None) is resolution.resolve_locality_permitted:
            return SimpleNamespace(context_window=WINDOW)
        if function is boundary_snapshot:
            return None, None, None
        if function is turn_iteration_context._read_native_checkpoint_context:
            return holder["context"]
        raise AssertionError(f"unexpected DB call {function!r}")

    async def admission_db(function, *args):
        assert function is checkpoint_native_admission.build_native_cold_checkpoint_admission
        checkpoint_id, thread_id, _context, turn = args[:4]
        return _Admission(_Request(checkpoint_id, thread_id, turn.turn_id, "cold"))

    def build_warm_admission(*, checkpoint_id, thread_id, turn, **_kwargs):
        return _Admission(_Request(checkpoint_id, thread_id, turn.turn_id, "warm"))

    async def request_checkpoint(request, _provider_request):
        requested.append(request.mode)
        if request.mode == "cold" and requested_raises["cold"]:
            raise _Started
        return SimpleNamespace(id=request.id, status="running")

    requested_raises = {"cold": False}
    holder = {"context": context}
    monkeypatch.setattr(turn_iteration_context, "provider_boundary_snapshot", boundary_snapshot)
    monkeypatch.setattr(turn_iteration_context, "run_in_db", iteration_db)
    monkeypatch.setattr(checkpoint_native_admission, "run_in_db", admission_db)
    monkeypatch.setattr(
        "core.checkpoint_native_fork.capture_live_warm_source",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr("core.checkpoint_warm_request.build_warm_admission", build_warm_admission)
    monkeypatch.setattr("core.checkpoint_runtime.request_checkpoint", request_checkpoint)
    return SimpleNamespace(requested=requested, raises=requested_raises, holder=holder)


@pytest.fixture
def log_lines():
    from loguru import logger

    lines: list[str] = []
    sink = logger.add(lines.append, format="{level}|{message}", level="DEBUG")
    yield lines
    logger.remove(sink)


def _logged(lines, level: str, text: str) -> bool:
    return any(line.startswith(f"{level}|") and text in line for line in lines)


async def _boundary(state, identity):
    from core.native_turn_control import TurnCancelEvent
    from core.turn_iteration_context import maintain_native_checkpoint

    return await maintain_native_checkpoint(
        agent=SimpleNamespace(), state=state, thread_id=THREAD,
        turn_identity=identity, cancel_event=TurnCancelEvent(),
        turn_model="native/test", reasoning_effort=None, project_id=None,
    )


@pytest.mark.asyncio
async def test_optional_initial_fold_waits_for_the_first_calls_warm_receipt(folds):
    from core.turn_core import TurnIdentity

    identity = TurnIdentity("turn-1", 1)
    state = _state(95_000)

    assert await _boundary(state, identity) is None
    assert folds.requested == []

    # The first call's correlated receipt, as _call_provider records it.
    state.iteration = 1
    state.checkpoint_source_messages = list(state.messages)
    state.last_measured_call_usage = {"prompt_tokens": 95_000}
    state.last_measured_rendered_input_tokens = 95_000

    assert await _boundary(state, identity) is None
    assert folds.requested == ["warm"]


@pytest.mark.asyncio
async def test_optional_fold_after_a_usage_less_call_is_skipped(folds, log_lines):
    from core.turn_core import TurnIdentity

    state = _state(95_000)
    # A later call ran but reported no usage: messages exist, measurement does not.
    state.iteration = 1
    state.checkpoint_source_messages = list(state.messages)

    assert await _boundary(state, TurnIdentity("turn-1", 1)) is None

    assert folds.requested == []
    assert _logged(log_lines, "INFO", "Optional checkpoint skipped")
    assert not any(line.startswith("WARNING|") for line in log_lines)


@pytest.mark.asyncio
async def test_planned_skip_logs_info_and_a_real_admission_failure_warns(folds, log_lines):
    from core.turn_core import TurnIdentity

    identity = TurnIdentity("turn-1", 1)
    assert await _boundary(_state(95_000), identity) is None
    assert _logged(log_lines, "INFO", "Optional checkpoint skipped")
    assert not _logged(log_lines, "WARNING", "Optional checkpoint unavailable")

    # With a receipt but no admissible context, admission really fails.
    folds.holder["context"] = None
    state = _state(95_000)
    state.iteration = 1
    state.checkpoint_source_messages = list(state.messages)
    state.last_measured_call_usage = {"prompt_tokens": 95_000}
    state.last_measured_rendered_input_tokens = 95_000

    assert await _boundary(state, identity) is None
    assert folds.requested == []
    assert _logged(log_lines, "WARNING", "Optional checkpoint unavailable")


@pytest.mark.asyncio
async def test_required_initial_fold_still_runs_cold(folds):
    from core.turn_core import TurnIdentity

    folds.raises["cold"] = True

    with pytest.raises(_Started):
        await _boundary(_state(120_000), TurnIdentity("turn-1", 1))

    assert folds.requested == ["cold"]


@pytest.mark.asyncio
async def test_single_call_turn_under_pressure_ends_without_a_fold(folds, monkeypatch):
    from core import turn_iteration
    from core.native_turn_control import TurnCancelEvent
    from core.turn_core import TurnIdentity

    response = SimpleNamespace(
        content="done", finish_reason="stop", has_tool_calls=False,
        usage={"prompt_tokens": 95_000}, reasoning_content=None,
        reasoning_provenance=None,
    )

    async def call_provider(*_args, **_kwargs):
        return response, False

    monkeypatch.setattr(turn_iteration, "_call_provider", call_provider)
    frames: list[str] = []

    async def on_step(kind, _payload):
        frames.append(kind)

    identity = TurnIdentity("turn-1", 1)
    cancel_event = TurnCancelEvent()
    cancel_event.bind_turn_identity(identity)
    agent = SimpleNamespace(max_iterations=5, _strip_think=lambda content: content)

    iterations, _tools = await turn_iteration._run_loop(
        agent, _state(95_000), THREAD, "native/test", {}, cancel_event, on_step,
        None, None, None, identity, None,
    )

    assert iterations == 1
    assert frames[-1] == "turn_complete"
    assert "error" not in frames
    assert folds.requested == []
