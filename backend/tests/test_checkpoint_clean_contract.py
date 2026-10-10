"""Cross-harness contracts for the clean checkpoint decision boundary."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest


@dataclass(frozen=True)
class DecisionCase:
    name: str
    input_tokens: int
    minimum_tokens: int
    checkpoint_status: str | None
    installed_input_tokens: int | None
    checkpoint_admissible: bool
    expected: str


DECISION_CASES = (
    DecisionCase("ordinary", 200_000, 8_000, None, None, True, "proceed"),
    DecisionCase(
        "soft pressure", 237_000, 8_000, None, None, True, "proceed_and_fold"
    ),
    DecisionCase(
        "soft pressure without spare capacity",
        237_000,
        8_000,
        None,
        None,
        False,
        "proceed",
    ),
    DecisionCase("hard wall", 262_145, 8_000, "running", None, True, "wait"),
    DecisionCase(
        "installed floor fits",
        262_145,
        8_000,
        "installed",
        24_000,
        True,
        "adopt_and_proceed",
    ),
    DecisionCase(
        "minimum replay cannot fit",
        300_000,
        270_000,
        None,
        None,
        True,
        "fail_visible",
    ),
)


def _decision_value(value: object) -> object:
    return getattr(value, "value", value)


@pytest.mark.parametrize("case", DECISION_CASES, ids=lambda case: case.name)
def test_all_harnesses_share_one_boundary_decision(case) -> None:
    from core.checkpoint_fit import ProviderPayload, evaluate_boundary

    payload = ProviderPayload(
        context_window=262_144,
        input_tokens=case.input_tokens,
        canonical_input_tokens=case.input_tokens,
        minimum_tokens=case.minimum_tokens,
        checkpoint_status=case.checkpoint_status,
        installed_input_tokens=case.installed_input_tokens,
        checkpoint_admissible=case.checkpoint_admissible,
    )

    assert _decision_value(evaluate_boundary(payload)) == case.expected


@pytest.mark.parametrize(
    ("window", "expected"),
    (
        (32_768, 8_192),
        (65_536, 16_384),
        (131_072, 28_672),
        (262_144, 28_672),
        (1_048_576, 32_768),
    ),
)
def test_soft_pressure_is_one_static_bounded_formula(window, expected) -> None:
    from core.checkpoint_fit import soft_pressure_remaining

    assert soft_pressure_remaining(window) == expected


@pytest.mark.parametrize("window", (131_072, 200_000, 262_144))
def test_soft_zone_entry_leaves_a_warm_fork_room_to_finish(window) -> None:
    """R08: soft pressure starts a checkpoint with room to finish, one tool round trip included."""
    from core.checkpoint_fit import (
        CHECKPOINT_INSTRUCTION_BUDGET_TOKENS,
        ProviderPayload,
        evaluate_boundary,
        soft_pressure_remaining,
    )
    from core.checkpoint_types import CHECKPOINT_OUTPUT_TOKENS, CHECKPOINT_TOOL_ROUND_TRIP_TOKENS

    entry = window - soft_pressure_remaining(window)
    payload = ProviderPayload(
        context_window=window,
        canonical_input_tokens=entry,
        input_tokens=entry,
        minimum_tokens=1,
        checkpoint_status=None,
        installed_input_tokens=None,
        checkpoint_admissible=True,
    )

    assert _decision_value(evaluate_boundary(payload)) == "proceed_and_fold"
    fork = (
        entry + CHECKPOINT_INSTRUCTION_BUDGET_TOKENS
        + CHECKPOINT_OUTPUT_TOKENS + CHECKPOINT_TOOL_ROUND_TRIP_TOKENS
    )
    assert fork <= window


@pytest.mark.parametrize(
    ("window", "fits"),
    ((32_768, False), (65_536, False), (114_687, False), (114_688, True)),
)
def test_warm_fork_room_is_a_stated_limit_below_115k(window, fits) -> None:
    """R08 holds from 114,688 up; below it the quarter cap wins, by design.

    Raising the reserve past a quarter of the window would fold a small model
    at low occupancy and loop, so its warm fork at soft entry cannot fit.
    """
    from core.checkpoint_fit import WARM_FORK_RESERVE_TOKENS, soft_pressure_remaining

    assert WARM_FORK_RESERVE_TOKENS * 4 == 114_688
    assert soft_pressure_remaining(window) <= max(window // 4, 8_192)
    assert (soft_pressure_remaining(window) >= WARM_FORK_RESERVE_TOKENS) is fits


def test_live_wall_reserves_no_arbitrary_output_budget() -> None:
    from core.checkpoint_fit import ProviderPayload, evaluate_boundary

    exact_fit = ProviderPayload(
        context_window=262_144,
        input_tokens=262_144,
        canonical_input_tokens=262_144,
        minimum_tokens=8_000,
        checkpoint_status=None,
        installed_input_tokens=None,
        checkpoint_admissible=False,
    )

    assert _decision_value(evaluate_boundary(exact_fit)) == "proceed"


def test_capped_window_above_live_usage_waits_then_adopts() -> None:
    from core.checkpoint_fit import ProviderPayload, evaluate_boundary

    waiting = ProviderPayload(
        context_window=262_144,
        input_tokens=300_000,
        canonical_input_tokens=300_000,
        minimum_tokens=8_000,
        checkpoint_status="running",
        installed_input_tokens=None,
        checkpoint_admissible=True,
    )
    installed = ProviderPayload(
        context_window=262_144,
        input_tokens=300_000,
        canonical_input_tokens=300_000,
        minimum_tokens=8_000,
        checkpoint_status="installed",
        installed_input_tokens=64_000,
        checkpoint_admissible=True,
    )

    assert _decision_value(evaluate_boundary(waiting)) == "wait"
    assert _decision_value(evaluate_boundary(installed)) == "adopt_and_proceed"


@pytest.mark.asyncio
async def test_new_installed_generation_adopts_from_boundary_snapshot(
    monkeypatch,
) -> None:
    """An install seen by the core read cannot permit old-context generation."""
    from core import checkpoint_resume
    from core.checkpoint_fit import BoundaryDecision
    from core.checkpoint_resume import InstalledContinuation
    from core.turn_core import TurnIdentity

    record = SimpleNamespace(id="checkpoint-new")

    async def render(installed):
        assert installed is record
        return InstalledContinuation(40, "new-context")

    boundary = SimpleNamespace(
        thread_id="thread-1",
        turn=TurnIdentity("turn-1", 1),
        context_window=100,
        canonical_input_tokens=50,
        provider_input_tokens=50,
        minimum_input_tokens=10,
        prepare_checkpoint=None,
        render_installed=render,
        wait_scope=SimpleNamespace(checkpoint_id=None),
        adopted_checkpoint_id="checkpoint-old",
        active_checkpoint=None,
        installed_checkpoint=record,
        eager_checkpoint_id=record.id,
    )

    continuation = await checkpoint_resume.maintain_before_provider_call(boundary)

    assert continuation.decision is BoundaryDecision.ADOPT_AND_PROCEED
    assert continuation.payload == "new-context"
    assert continuation.checkpoint_id == record.id


@pytest.mark.asyncio
async def test_same_installed_generation_does_not_adopt_twice() -> None:
    from core import checkpoint_resume
    from core.checkpoint_fit import BoundaryDecision
    from core.turn_core import TurnIdentity

    record = SimpleNamespace(id="checkpoint-current", status="installed")

    async def reject_render(_record):
        raise AssertionError("an already adopted checkpoint must not render again")

    boundary = SimpleNamespace(
        thread_id="thread-1",
        turn=TurnIdentity("turn-1", 1),
        context_window=100,
        canonical_input_tokens=50,
        provider_input_tokens=50,
        minimum_input_tokens=10,
        prepare_checkpoint=None,
        render_installed=reject_render,
        wait_scope=SimpleNamespace(checkpoint_id=None),
        adopted_checkpoint_id=record.id,
        active_checkpoint=None,
        installed_checkpoint=record,
        eager_checkpoint_id=record.id,
    )

    continuation = await checkpoint_resume.maintain_before_provider_call(boundary)

    assert continuation.decision is BoundaryDecision.PROCEED
    assert continuation.payload is None


@pytest.mark.asyncio
async def test_hard_wall_takes_one_newer_fold_when_installed_tail_no_longer_fits(
    monkeypatch,
) -> None:
    from core import checkpoint_resume, checkpoint_runtime
    from core.checkpoint_resume import (
        CheckpointAdmission,
        CheckpointWaitScope,
        InstalledContinuation,
        ProviderBoundary,
    )
    from core.turn_core import TurnIdentity

    old = SimpleNamespace(
        id="checkpoint-old", status="running", error=None, originating_turn_id="turn-1",
    )
    new = SimpleNamespace(
        id="checkpoint-new", status="running", error=None, originating_turn_id="turn-1",
    )
    awaited: list[str] = []
    prepared: list[bool] = []

    async def await_checkpoint(checkpoint_id):
        awaited.append(checkpoint_id)
        record = old if checkpoint_id == old.id else new
        record.status = "installed"
        return record

    async def request_checkpoint(request, _provider_request):
        assert request.id != old.id
        new.id = request.id
        return new

    async def prepare(checkpoint_id, required):
        prepared.append(required)
        request = SimpleNamespace(
            id=checkpoint_id,
            thread_id="thread-1",
            originating_turn_id="turn-1",
        )
        return CheckpointAdmission(request, object())

    async def render(record):
        tokens = 120 if record is old else 40
        return InstalledContinuation(tokens, f"payload:{record.id}")

    monkeypatch.setattr(checkpoint_runtime, "await_checkpoint", await_checkpoint)
    monkeypatch.setattr(checkpoint_runtime, "request_checkpoint", request_checkpoint)
    boundary = ProviderBoundary(
        thread_id="thread-1",
        turn=TurnIdentity("turn-1", 1),
        context_window=100,
        canonical_input_tokens=110,
        provider_input_tokens=110,
        minimum_input_tokens=10,
        adopted_checkpoint_id=None,
        active_checkpoint=old,
        installed_checkpoint=None,
        prepare_checkpoint=prepare,
        render_installed=render,
        wait_scope=CheckpointWaitScope(),
    )

    continuation = await checkpoint_resume.maintain_before_provider_call(boundary)

    assert continuation.payload == f"payload:{new.id}"
    assert awaited == [old.id, new.id]
    assert prepared == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("fold_outcome", ("refused", "timeout"))
async def test_stop_releases_hard_wait_without_cancelling_background_fold(
    monkeypatch,
    fold_outcome,
) -> None:
    from core import checkpoint_resume, checkpoint_runtime
    from core.checkpoint_resume import (
        CheckpointWaitScope,
        InstalledContinuation,
        ProviderBoundary,
    )
    from core.turn_core import TurnIdentity

    # Started outside this Turn (idle, Checkpoint Now, or an earlier Turn), so
    # it stays independent of Stop even while this Turn is blocked on it.
    record = SimpleNamespace(
        id="checkpoint-background", status="running", error=None,
        originating_turn_id=None,
    )
    waiting = asyncio.Event()
    finish_checkpoint = asyncio.Event()
    checkpoint_finished = asyncio.Event()
    scope = CheckpointWaitScope()

    async def checkpoint_operation():
        await finish_checkpoint.wait()
        record.status = "failed"
        record.error = "provider refused checkpoint"
        checkpoint_finished.set()

    operation = asyncio.create_task(checkpoint_operation())

    async def await_checkpoint(_checkpoint_id):
        waiting.set()
        await asyncio.shield(operation)
        return record

    async def cancel_checkpoint(checkpoint_id):
        raise AssertionError(f"Stop cancelled checkpoint {checkpoint_id}")

    async def render(_record):
        return InstalledContinuation(50, "payload")

    monkeypatch.setattr(checkpoint_runtime, "await_checkpoint", await_checkpoint)
    monkeypatch.setattr(checkpoint_runtime, "cancel_checkpoint", cancel_checkpoint)
    boundary = ProviderBoundary(
        thread_id="thread-1",
        turn=TurnIdentity("turn-1", 1),
        context_window=100,
        canonical_input_tokens=110,
        provider_input_tokens=110,
        minimum_input_tokens=10,
        adopted_checkpoint_id=None,
        active_checkpoint=record,
        installed_checkpoint=None,
        prepare_checkpoint=None,
        render_installed=render,
        wait_scope=scope,
    )

    hard_wait = asyncio.create_task(
        checkpoint_resume.maintain_before_provider_call(boundary)
    )
    await waiting.wait()
    assert scope.checkpoint_id == record.id
    scope.request_stop()
    with pytest.raises(asyncio.CancelledError):
        await hard_wait
    assert scope.checkpoint_id is None
    assert not checkpoint_finished.is_set()
    assert not operation.done()

    if fold_outcome == "refused":
        finish_checkpoint.set()
        await checkpoint_finished.wait()
        await operation
        assert record.status == "failed"
    else:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.parametrize(
    ("raw_body", "expected"),
    (("plain prose", True), ("{broken json", True), ("", False), ("  \n\t", False)),
)
def test_checkpoint_acceptance_depends_only_on_nonblank_narrative(
    raw_body, expected
) -> None:
    # Staged edits alone no longer make a blank narrative installable (#1767):
    # a blank raw_result would silently discard the history it claims to cover.
    from core.checkpoint_render import usable_result

    assert usable_result(raw_body) is expected
