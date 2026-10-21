"""Journey contracts for checkpoints beneath one logical CORSIA Turn."""

from __future__ import annotations

from core.checkpoint_types import CheckpointProviderForkRequest

import asyncio
from types import SimpleNamespace

import pytest

from core.fork_types import (
    ProviderForkRequest,
    ProviderForkResult,
)
from core.checkpoint_types import CheckpointRequest
from core.turn_core import TurnIdentity
from llm.resolution import ExecutionTarget


def _target() -> ExecutionTarget:
    return ExecutionTarget(
        catalog_id="openai/checkpoint-resume",
        harness="native",
        provider="openai",
        backend="api",
        transport_model="checkpoint-resume",
        expected_reported_model=None,
        context_window=100_000,
        available=True,
        availability_reason=None,
        display_name="checkpoint-resume",
        supports_reasoning=True,
        preserve_thinking_default=True,
    )


def _seed(thread_id: str) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads "
            "(id,user_id,model,history_epoch,next_message_seq,created_at,updated_at) "
            "VALUES (?, 'user-1', 'openai/checkpoint-resume', 0, 4, 't0', 't0')",
            (thread_id,),
        )
        for seq, role in ((1, "user"), (2, "assistant"), (3, "tool")):
            db.execute(
                "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
                "VALUES (?,?,?,?, 't0', ?)",
                (f"{thread_id}-m{seq}", thread_id, role, f"body-{seq}", seq),
            )
        db.commit()
    finally:
        db.close()


def _admission(thread_id: str, checkpoint_id: str, turn: TurnIdentity):
    from core.checkpoint_resume import CheckpointAdmission

    target = _target()
    request = CheckpointRequest(
        id=checkpoint_id,
        thread_id=thread_id,
        history_epoch=0,
        previous_generation_id=None,
        through_seq=3,
        through_message_id=f"{thread_id}-m3",
        trigger="pressure",
        mode="warm",
        source_harness="native",
        source_model=target.catalog_id,
        writer_harness="native",
        writer_model=target.catalog_id,
        originating_turn_id=turn.turn_id,
        first_included_seq=None,
        started_at="t1",
    )
    provider = CheckpointProviderForkRequest(
        fork_id=checkpoint_id,
        thread_id=thread_id,
        target=target,
        instruction="Write one checkpoint.",
        reasoning_effort=None,
        working_dir="/tmp",
        warm_source=object(),
    )
    return CheckpointAdmission(request, provider)


@pytest.mark.asyncio
async def test_hard_wait_installs_once_and_returns_same_turn_continuation(
    isolated_db,
    monkeypatch,
) -> None:
    from core import checkpoint_runtime
    from core.fork_fit import BoundaryDecision
    from core.checkpoint_resume import (
        CheckpointWaitScope,
        InstalledContinuation,
        ProviderBoundary,
        maintain_before_provider_call,
    )

    _seed("hard-wait")
    turn = TurnIdentity("turn-1", 1)
    release = asyncio.Event()
    started = asyncio.Event()
    scope = CheckpointWaitScope()

    async def invoke(_request, _cancel):
        started.set()
        await release.wait()
        return ProviderForkResult("installed body", None)

    async def prepare(checkpoint_id: str, required: bool):
        assert required is True
        return _admission("hard-wait", checkpoint_id, turn)

    async def render(record):
        return InstalledContinuation(12_000, {"checkpoint_id": record.id})

    monkeypatch.setattr(checkpoint_runtime, "_invoke", invoke)
    boundary = ProviderBoundary(
        thread_id="hard-wait",
        turn=turn,
        context_window=100_000,
        canonical_input_tokens=100_001,
        provider_input_tokens=None,
        minimum_input_tokens=2_000,
        adopted_checkpoint_id=None,
        active_checkpoint=None,
        installed_checkpoint=None,
        prepare_checkpoint=prepare,
        render_installed=render,
        wait_scope=scope,
    )
    waiting = asyncio.create_task(maintain_before_provider_call(boundary))
    await started.wait()

    assert scope.checkpoint_id is not None
    assert waiting.done() is False
    release.set()
    continuation = await waiting

    assert continuation.decision is BoundaryDecision.ADOPT_AND_PROCEED
    assert continuation.turn == turn
    assert continuation.payload == {"checkpoint_id": continuation.checkpoint_id}
    assert scope.checkpoint_id is None


@pytest.mark.asyncio
async def test_soft_pressure_starts_background_checkpoint_without_stop_ownership(
    isolated_db,
    monkeypatch,
) -> None:
    from core import checkpoint_runtime
    from core.fork_fit import BoundaryDecision
    from core.checkpoint_resume import (
        CheckpointWaitScope,
        InstalledContinuation,
        ProviderBoundary,
        maintain_before_provider_call,
    )

    _seed("background")
    turn = TurnIdentity("turn-background", 1)
    release = asyncio.Event()
    scope = CheckpointWaitScope()

    async def invoke(_request, _cancel):
        await release.wait()
        return ProviderForkResult("background body", None)

    async def prepare(checkpoint_id: str, required: bool):
        assert required is False
        return _admission("background", checkpoint_id, turn)

    async def render(_record):
        return InstalledContinuation(12_000, object())

    monkeypatch.setattr(checkpoint_runtime, "_invoke", invoke)
    result = await maintain_before_provider_call(ProviderBoundary(
        thread_id="background",
        turn=turn,
        context_window=100_000,
        canonical_input_tokens=95_000,
        provider_input_tokens=None,
        minimum_input_tokens=2_000,
        adopted_checkpoint_id=None,
        active_checkpoint=None,
        installed_checkpoint=None,
        prepare_checkpoint=prepare,
        render_installed=render,
        wait_scope=scope,
    ))

    assert result.decision is BoundaryDecision.PROCEED_AND_FOLD
    assert result.turn == turn
    assert scope.checkpoint_id is None
    assert result.checkpoint_id is not None
    assert (await checkpoint_runtime._read(result.checkpoint_id)).status == "running"
    release.set()
    assert (await checkpoint_runtime.await_checkpoint(result.checkpoint_id)).status == "installed"


@pytest.mark.asyncio
async def test_hard_wait_joiner_detaches_without_cancelling_background_work(
    isolated_db,
    monkeypatch,
) -> None:
    from core import checkpoint_runtime
    from core.checkpoint_resume import (
        CheckpointWaitScope,
        InstalledContinuation,
        ProviderBoundary,
        maintain_before_provider_call,
    )

    _seed("joiner")
    turn = TurnIdentity("turn-joiner", 1)
    release = asyncio.Event()
    scope = CheckpointWaitScope()

    async def invoke(_request, _cancel):
        await release.wait()
        return ProviderForkResult("background body", None)

    async def render(_record):
        return InstalledContinuation(12_000, object())

    monkeypatch.setattr(checkpoint_runtime, "_invoke", invoke)
    admission = _admission("joiner", "background-cp", turn)
    await checkpoint_runtime.request_checkpoint(
        admission.request,
        admission.provider_request,
    )
    waiting = asyncio.create_task(maintain_before_provider_call(ProviderBoundary(
        thread_id="joiner",
        turn=turn,
        context_window=100_000,
        canonical_input_tokens=100_001,
        provider_input_tokens=None,
        minimum_input_tokens=2_000,
        adopted_checkpoint_id=None,
        # This Turn's own attempt: only Stop, not a detaching cancel, ends it.
        active_checkpoint=SimpleNamespace(
            id="background-cp", status="running", error=None,
            originating_turn_id=turn.turn_id,
        ),
        installed_checkpoint=None,
        prepare_checkpoint=None,
        render_installed=render,
        wait_scope=scope,
    )))
    await asyncio.sleep(0)

    assert scope.checkpoint_id == "background-cp"
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert (await checkpoint_runtime._read("background-cp")).status == "running"
    release.set()
    assert (await checkpoint_runtime.await_checkpoint("background-cp")).status == "installed"
