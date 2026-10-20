"""External adapters advancing after an earlier checkpoint installs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.checkpoint_resume import CheckpointAdmission, CheckpointWaitScope
from core.checkpoint_types import (
    CheckpointRequest,
    ProviderCheckpointRequest,
    ProviderCheckpointResult,
)
from core.turn_core import TurnIdentity
from history.types import HistoryBoundary
from llm.resolution import ExecutionTarget
from subagents.harness_checkpoint_context import ExternalCheckpointContext


def _target(harness: str) -> ExecutionTarget:
    return ExecutionTarget(
        catalog_id=f"{harness}/awaited-advance",
        harness=harness,
        provider="anthropic" if harness == "claude-code" else "openai",
        backend="cli",
        transport_model="awaited-advance",
        expected_reported_model=None,
        context_window=100_000,
        available=True,
        availability_reason=None,
        display_name="awaited-advance",
        supports_reasoning=True,
        preserve_thinking_default=True,
    )


def _seed(thread_id: str, model: str) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads "
            "(id,user_id,model,history_epoch,next_message_seq,created_at,updated_at) "
            "VALUES (?, 'user-1', ?, 0, 5, 't0', 't0')",
            (thread_id, model),
        )
        for seq, role in (
            (1, "user"),
            (2, "assistant"),
            (3, "user"),
            (4, "assistant"),
        ):
            db.execute(
                "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
                "VALUES (?,?,?,?, 't0', ?)",
                (f"{thread_id}-m{seq}", thread_id, role, f"body-{seq}", seq),
            )
        db.commit()
    finally:
        db.close()


def _earlier_admission(
    thread_id: str,
    checkpoint_id: str,
    target: ExecutionTarget,
    turn: TurnIdentity,
) -> CheckpointAdmission:
    request = CheckpointRequest(
        id=checkpoint_id,
        thread_id=thread_id,
        history_epoch=0,
        previous_generation_id=None,
        through_seq=2,
        through_message_id=f"{thread_id}-m2",
        trigger="pressure",
        mode="warm",
        source_harness=target.harness,
        source_model=target.catalog_id,
        writer_harness=target.harness,
        writer_model=target.catalog_id,
        originating_turn_id=turn.turn_id,
        first_included_seq=None,
        started_at="t1",
    )
    return CheckpointAdmission(
        request,
        ProviderCheckpointRequest(
            fork_id=checkpoint_id,
            thread_id=thread_id,
            target=target,
            instruction="Write one checkpoint.",
            reasoning_effort=None,
            working_dir="/tmp",
            warm_source=object(),
        ),
    )


@pytest.fixture(autouse=True)
async def _clean_checkpoint_runtime(monkeypatch):
    from core import checkpoint_runtime
    from infra import thread_execution

    async def no_redrive(_thread_id):
        return None

    # These tests prove the cut, not the FIFO re-drive after it.
    monkeypatch.setattr(thread_execution, "drive_thread", no_redrive)
    await checkpoint_runtime.shutdown_checkpoint_tasks()
    yield
    await checkpoint_runtime.shutdown_checkpoint_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ("claude-code", "codex"))
async def test_external_tool_boundary_advances_after_awaited_earlier_cut(
    isolated_db,
    monkeypatch,
    harness,
) -> None:
    """An oversized awaited replay prepares from its newly installed predecessor."""
    from core import checkpoint_resume, checkpoint_runtime
    import infra.checkpoints as store
    import infra.checkpoint_queries as query_store
    from infra.database import get_db
    from subagents import harness_checkpoint_resume as bridge

    thread_id = f"awaited-advance-{harness}"
    earlier_id = f"checkpoint-earlier-{harness}"
    target = _target(harness)
    turn = TurnIdentity(f"turn-{harness}", 1)
    _seed(thread_id, target.catalog_id)
    release_earlier = asyncio.Event()
    provider_requests = []
    captured_predecessors = []
    fresh_warm_source = object()
    resumed = []

    async def invoke(request, _cancel):
        provider_requests.append(request)
        if request.checkpoint_id == earlier_id:
            await release_earlier.wait()
        return ProviderCheckpointResult('{"summary":"installed"}', None)

    def read_context(*_args, **_kwargs):
        head = query_store.current_installed(thread_id)
        return ExternalCheckpointContext(
            thread_row={},
            target=target,
            history=SimpleNamespace(),
            boundary=HistoryBoundary(thread_id, 0, 4, f"{thread_id}-m4"),
            previous_generation_id=head.id if head is not None else None,
            working_dir="/tmp",
            tail_tokens=1_000,
            minimum_tail_tokens=1_000,
            active_checkpoint=query_store.active_for_thread(thread_id),
            warm_boundary=SimpleNamespace(
                role="assistant", content="body-4", tool_call_id=None,
            ),
        )

    async def capture(context, *_args, **_kwargs):
        captured_predecessors.append(context.previous_generation_id)
        return fresh_warm_source

    def replay(record):
        return SimpleNamespace(checkpoint_generation_id=record.id)

    async def render(_context, _turn, selection, _input_identity):
        tokens = 120_001 if selection.checkpoint_generation_id == earlier_id else 12_000
        return SimpleNamespace(rendered=SimpleNamespace(input_tokens=tokens))

    async def resume(_live, _thread_id, prepared, _harness):
        resumed.append(prepared)

    real_await = checkpoint_runtime.await_checkpoint

    async def await_and_install(checkpoint_id):
        if checkpoint_id == earlier_id:
            release_earlier.set()
        return await real_await(checkpoint_id)

    monkeypatch.setattr(checkpoint_runtime, "_invoke", invoke)
    monkeypatch.setattr(checkpoint_runtime, "await_checkpoint", await_and_install)
    monkeypatch.setattr(checkpoint_resume, "installed_replay", replay)
    monkeypatch.setattr(bridge, "read_external_checkpoint_context", read_context)
    monkeypatch.setattr(
        bridge, "external_measurement", lambda *_args: (120_000, 100_000),
    )
    monkeypatch.setattr(
        bridge, "_capture_external_tool_boundary_warm_source", capture,
    )
    monkeypatch.setattr(bridge, "render_external_selection", render)
    monkeypatch.setattr(bridge, "_resume_external", resume)

    earlier = _earlier_admission(thread_id, earlier_id, target, turn)
    active = await checkpoint_runtime.request_checkpoint(
        earlier.request, earlier.provider_request,
    )
    assert active.status == "running"

    live = SimpleNamespace(
        corsia_turn=turn,
        checkpoint_wait_scope=CheckpointWaitScope(),
        catalog_model_id=target.catalog_id,
        session=SimpleNamespace(),
    )
    assert await bridge.maintain_external_tool_boundary(
        live, thread_id, harness,
    )

    assert captured_predecessors == [earlier_id]
    assert len(provider_requests) == 2
    assert provider_requests[1].warm_source is fresh_warm_source
    assert len(resumed) == 1
    head = query_store.current_installed(thread_id)
    assert head.previous_generation_id == earlier_id
    assert (head.through_seq, head.through_message_id) == (
        4, f"{thread_id}-m4",
    )
    db = get_db()
    try:
        assert all(
            db.execute(
                "SELECT COUNT(*) FROM messages WHERE id=?",
                (store.completion_receipt_id(checkpoint_id),),
            ).fetchone()[0] == 1
            for checkpoint_id in (earlier_id, head.id)
        )
    finally:
        db.close()
    assert checkpoint_runtime.checkpoint_task_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ("claude-code", "codex"))
async def test_external_initial_boundary_advances_after_awaited_earlier_cut(
    isolated_db,
    monkeypatch,
    harness,
) -> None:
    """Initial provider admission also prepares from the installed predecessor."""
    from core import checkpoint_runtime
    from core.checkpoint_resume import InstalledContinuation
    from core.checkpoint_warm_request import build_warm_admission
    import infra.checkpoints as store
    import infra.checkpoint_queries as query_store
    from subagents import harness_checkpoint_resume as bridge

    thread_id = f"awaited-initial-{harness}"
    earlier_id = f"checkpoint-initial-earlier-{harness}"
    target = _target(harness)
    turn = TurnIdentity(f"turn-initial-{harness}", 1)
    _seed(thread_id, target.catalog_id)
    release_earlier = asyncio.Event()
    provider_requests = []
    prepared_predecessors = []
    preparation_source = object()

    async def invoke(request, _cancel):
        provider_requests.append(request)
        if request.checkpoint_id == earlier_id:
            await release_earlier.wait()
        return ProviderCheckpointResult('{"summary":"installed"}', None)

    def read_context(*_args, **_kwargs):
        head = query_store.current_installed(thread_id)
        return ExternalCheckpointContext(
            thread_row={},
            target=target,
            history=SimpleNamespace(),
            boundary=HistoryBoundary(thread_id, 0, 4, f"{thread_id}-m4"),
            previous_generation_id=head.id if head is not None else None,
            working_dir="/tmp",
            tail_tokens=1_000,
            minimum_tail_tokens=1_000,
            active_checkpoint=query_store.active_for_thread(thread_id),
            previous=head,
        )

    async def prepare(checkpoint_id, required, *, context, **_kwargs):
        assert required is True
        prepared_predecessors.append(context.previous_generation_id)
        return build_warm_admission(
            checkpoint_id=checkpoint_id,
            thread_id=thread_id,
            turn=turn,
            target=target,
            boundary=context.boundary,
            previous_generation_id=context.previous_generation_id,
            working_dir="/tmp",
            reasoning_effort=None,
            binary=None,
            warm_source=preparation_source,
        )

    async def render(record, *, refresh_prepared):
        tokens = 120_001 if record.id == earlier_id else 12_000
        return InstalledContinuation(
            tokens,
            SimpleNamespace(
                checkpoint_generation_id=record.id,
                refresh_prepared=refresh_prepared,
            ),
        )

    real_await = checkpoint_runtime.await_checkpoint

    async def await_and_install(checkpoint_id):
        if checkpoint_id == earlier_id:
            release_earlier.set()
        return await real_await(checkpoint_id)

    monkeypatch.setattr(checkpoint_runtime, "_invoke", invoke)
    monkeypatch.setattr(checkpoint_runtime, "await_checkpoint", await_and_install)
    monkeypatch.setattr(bridge, "read_external_checkpoint_context", read_context)
    monkeypatch.setattr(bridge, "_prepare_initial_external_checkpoint", prepare)
    monkeypatch.setattr(bridge, "_render_initial_installed", render)

    earlier = _earlier_admission(thread_id, earlier_id, target, turn)
    await checkpoint_runtime.request_checkpoint(
        earlier.request, earlier.provider_request,
    )
    live = SimpleNamespace(
        corsia_turn=turn,
        checkpoint_wait_scope=CheckpointWaitScope(),
        catalog_model_id=target.catalog_id,
    )
    initial = SimpleNamespace(
        mode="exact_replay",
        fit_ledger=SimpleNamespace(
            rendered_input_tokens=120_001,
            effective_window_tokens=100_000,
            section_tokens=(("current_input", 500),),
        ),
    )
    continuation = await bridge.maintain_external_initial_boundary(
        live,
        thread_id,
        harness,
        initial,
        refresh_prepared=lambda _selection: None,
    )

    assert continuation.checkpoint_generation_id == query_store.current_installed(thread_id).id
    assert prepared_predecessors == [earlier_id]
    assert len(provider_requests) == 2
    assert provider_requests[1].warm_source is preparation_source
    head = query_store.current_installed(thread_id)
    assert head.previous_generation_id == earlier_id
    assert (head.through_seq, head.through_message_id) == (
        4, f"{thread_id}-m4",
    )
    assert checkpoint_runtime.checkpoint_task_count() == 0
