"""Installed-winner continuation contracts for checkpoint admission races."""

from __future__ import annotations

from dataclasses import replace

import pytest

from core.fork_types import (
    ProviderForkRequest,
    ProviderForkResult,
)
from core.checkpoint_types import CheckpointRequest
from core.turn_core import TurnIdentity
from llm.resolution import ExecutionTarget


def _target(harness: str = "native") -> ExecutionTarget:
    return ExecutionTarget(
        catalog_id=f"{harness}/checkpoint-resume",
        harness=harness,
        provider="anthropic" if harness == "claude-code" else "openai",
        backend="cli" if harness != "native" else "api",
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


def _admission(
    thread_id: str,
    checkpoint_id: str,
    turn: TurnIdentity,
    harness: str = "native",
):
    from core.checkpoint_resume import CheckpointAdmission

    target = _target(harness)
    return CheckpointAdmission(
        CheckpointRequest(
            id=checkpoint_id,
            thread_id=thread_id,
            history_epoch=0,
            previous_generation_id=None,
            through_seq=3,
            through_message_id=f"{thread_id}-m3",
            trigger="pressure",
            mode="warm",
            source_harness=harness,
            source_model=target.catalog_id,
            writer_harness=harness,
            writer_model=target.catalog_id,
            originating_turn_id=turn.turn_id,
            first_included_seq=None,
            started_at="t1",
        ),
        ProviderForkRequest(
            fork_id=checkpoint_id,
            thread_id=thread_id,
            target=target,
            instruction="Write one checkpoint.",
            reasoning_effort=None,
            working_dir="/tmp",
            warm_source=object(),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ("native", "claude-code", "codex"))
async def test_shared_hard_wait_adopts_installed_race_winner_without_stale_write(
    isolated_db,
    monkeypatch,
    harness,
) -> None:
    from core import checkpoint_runtime
    from core.checkpoint_resume import (
        CheckpointWaitScope,
        InstalledContinuation,
        ProviderBoundary,
        cancel_waiting_checkpoint,
        maintain_before_provider_call,
    )
    from infra import checkpoints as store
    from infra.database import get_db

    _seed("race-winner")
    turn = TurnIdentity("turn-race-winner", 1)
    scope = CheckpointWaitScope()
    provider_calls = 0
    real_create = store.create_running
    raced = False

    async def invoke(_request, _cancel):
        nonlocal provider_calls
        provider_calls += 1
        return ProviderForkResult("stale candidate", None)

    def create_with_winner(request):
        nonlocal raced
        if not raced:
            raced = True
            winner = real_create(
                replace(request, id="installed-winner")
            )
            store.capture(
                winner.id, raw_result="winner", usage_json=None, captured_at="t2",
            )
            store.install(winner.id, installed_at="t3")
        return real_create(request)

    async def prepare(checkpoint_id: str, required: bool):
        assert required is True
        return _admission("race-winner", checkpoint_id, turn, harness)

    async def render(record):
        return InstalledContinuation(12_000, {"checkpoint_id": record.id})

    monkeypatch.setattr(checkpoint_runtime, "_invoke", invoke)
    monkeypatch.setattr(store, "create_running", create_with_winner)
    continuation = await maintain_before_provider_call(ProviderBoundary(
        thread_id="race-winner",
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
    ))

    assert continuation.checkpoint_id == "installed-winner"
    assert continuation.payload == {"checkpoint_id": "installed-winner"}
    assert provider_calls == 0
    assert checkpoint_runtime.fork_task_count() == 0
    assert scope.checkpoint_id is None
    assert await cancel_waiting_checkpoint(scope) is None
    db = get_db()
    try:
        receipt_id = store.completion_receipt_id("installed-winner")
        count = db.execute(
            "SELECT COUNT(*) FROM messages WHERE id=?", (receipt_id,),
        ).fetchone()[0]
    finally:
        db.close()
    assert count == 1


@pytest.mark.asyncio
async def test_oversized_race_winner_allows_only_one_newer_fold(
    isolated_db,
    monkeypatch,
) -> None:
    from core import checkpoint_runtime
    from core.checkpoint_resume import (
        CheckpointAdmission,
        CheckpointWaitScope,
        InstalledContinuation,
        ProviderBoundary,
        maintain_before_provider_call,
    )
    from infra import checkpoints as store

    _seed("oversized-winner")
    turn = TurnIdentity("turn-oversized-winner", 1)
    provider_ids: list[str] = []
    real_create = store.create_running
    prepare_count = 0
    raced = False

    async def invoke(request, _cancel):
        provider_ids.append(request.fork_id)
        return ProviderForkResult("newer fold", None)

    def create_with_winner(request):
        nonlocal raced
        if not raced:
            raced = True
            winner = real_create(
                replace(request, id="oversized-installed-winner")
            )
            store.capture(
                winner.id, raw_result="winner", usage_json=None, captured_at="t2",
            )
            store.install(winner.id, installed_at="t3")
        return real_create(request)

    async def prepare(checkpoint_id: str, required: bool):
        nonlocal prepare_count
        assert required is True
        prepare_count += 1
        admission = _admission("oversized-winner", checkpoint_id, turn)
        if prepare_count == 1:
            return CheckpointAdmission(
                replace(
                    admission.request,
                    through_seq=2,
                    through_message_id="oversized-winner-m2",
                ),
                admission.provider_request,
            )
        return CheckpointAdmission(
            replace(
                admission.request,
                previous_generation_id="oversized-installed-winner",
            ),
            admission.provider_request,
        )

    async def render(record):
        tokens = 100_001 if record.id == "oversized-installed-winner" else 12_000
        return InstalledContinuation(tokens, {"checkpoint_id": record.id})

    async def notify(_record):
        return None

    monkeypatch.setattr(checkpoint_runtime, "_invoke", invoke)
    monkeypatch.setattr(checkpoint_runtime, "_notify_completion", notify)
    monkeypatch.setattr(store, "create_running", create_with_winner)
    continuation = await maintain_before_provider_call(ProviderBoundary(
        thread_id="oversized-winner",
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
        wait_scope=CheckpointWaitScope(),
    ))

    assert prepare_count == 2
    assert provider_ids == [continuation.checkpoint_id]
    assert continuation.payload == {"checkpoint_id": continuation.checkpoint_id}
