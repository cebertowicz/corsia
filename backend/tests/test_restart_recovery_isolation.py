"""Recovery preserves per-thread order without serializing unrelated providers."""

import asyncio
from types import SimpleNamespace

import pytest
from loguru import logger


@pytest.mark.asyncio
async def test_reconcile_releases_ingress_after_durable_fifo_claims(isolated_db, monkeypatch):
    """A provider that has not even started cannot keep an unrelated send held."""
    from automation import restart_startup, self_restart
    from infra.database import get_db
    from infra.thread_driver import admit_input

    db = get_db()
    try:
        for thread_id in ("recovering", "unrelated"):
            db.execute(
                "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
                "VALUES (?,'user-1','oMLX/qwen','t0','t0')", (thread_id,),
            )
        operation = self_restart.try_acquire(None, None, None, None, "restart", db=db)
        assert operation is not None
        assert self_restart.transition_operation(
            operation, expected="holding", target="steering", db=db,
        )
        first = admit_input(
            "recovering", role="user", content="first", intent="apply_now",
            message_id="first", turn_id="before", boot_epoch="old",
            admitted_at="t1", db=db,
        )
        assert first.held_for_restart
        db.commit()
    finally:
        db.close()

    recovered, _wakes = await restart_startup.reconcile_startup_continuity(logger)
    assert len(recovered.batches) == 1
    assert self_restart.operation_by_id(operation)["status"] == "complete"
    second = admit_input(
        "recovering", role="user", content="second", intent="queue_after_turn",
        message_id="second", turn_id="must-not-overtake", boot_epoch="new",
        admitted_at="t2",
    )
    other = admit_input(
        "unrelated", role="user", content="other", intent="apply_now",
        message_id="other", turn_id="other-turn", boot_epoch="new",
        admitted_at="t3",
    )
    assert not second.held_for_restart and second.batch is None
    assert other.batch is not None
    claimed = [item.message_id for item in recovered.batches[0].items]
    assert claimed[-1] == "first" and "second" not in claimed


@pytest.mark.asyncio
async def test_startup_broadcasts_committed_admission_failures(monkeypatch):
    """A failed startup FIFO prefix is visible without a duplicate generic wake."""
    from automation import restart_startup
    from infra import thread_execution, turn_admission_driver
    from infra.thread_input_types import FailedInputBatch

    broadcasts: list[tuple[str, tuple[str, ...]]] = []
    wakes: list[str] = []

    async def drive(admission):
        broadcasts.append((admission.thread_id, admission.message_ids))

    async def wake(thread_id):
        wakes.append(thread_id)

    async def replay():
        return None

    monkeypatch.setattr(turn_admission_driver, "drive_turn_admission", drive)
    monkeypatch.setattr(thread_execution, "drive_thread", wake)
    monkeypatch.setattr(restart_startup, "_replay_held_webhooks", replay)
    recovery = SimpleNamespace(
        settlements=(),
        batches=(FailedInputBatch("failed", ("input",), "plan admission failed"),),
    )

    await restart_startup.drive_startup_continuity(recovery, ("failed",), logger)

    assert broadcasts == [("failed", ("input",))]
    assert wakes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_stalled_or_failed_recovery_does_not_block_sibling(monkeypatch, failure):
    """Restored work, including legacy waits, progresses despite the first provider."""
    from automation import restart_startup
    from infra import thread_execution

    entered, sibling, release, legacy = (asyncio.Event() for _ in range(4))

    async def provider(batch):
        if batch.thread_id == "first":
            entered.set()
            if failure:
                raise RuntimeError("provider unavailable")
            await release.wait()
        elif batch.thread_id == "second":
            sibling.set()
        else:
            legacy.set()

    async def replay():
        return None

    monkeypatch.setattr(thread_execution, "drive_claimed_batch", provider)
    monkeypatch.setattr(restart_startup, "_replay_held_webhooks", replay)
    monkeypatch.setattr(restart_startup, "_complete_restart_operation", lambda: None)
    recovery = SimpleNamespace(
        settlements=(),
        batches=tuple(SimpleNamespace(thread_id=t, turn_id=t)
                      for t in ("first", "second", "legacy")),
    )
    task = asyncio.create_task(restart_startup.drive_startup_continuity(recovery, (), logger))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(sibling.wait(), 0.2)
        await asyncio.wait_for(legacy.wait(), 0.2)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_initial_codex_warm_capture_does_not_reacquire_held_transition_lock(monkeypatch):
    """Initial resume keeps warm capture without waiting on its own runtime lock."""
    from core import fork_codex, fork_render
    from subagents import harness_checkpoint_resume
    from subagents.codex_runtime_state import _LIVE

    thread_id = "initial-warm"
    live = SimpleNamespace(tool_step_ids={})
    context = SimpleNamespace(
        target=SimpleNamespace(harness="codex"), boundary=object(),
        warm_boundary=object(), binary="codex", reasoning_effort="high",
    )
    source = object()

    async def capture(*_args, **kwargs):
        assert kwargs["prefix_bytes"] == 4096
        return source

    async def prepare(_checkpoint_id, **kwargs):
        return kwargs["warm_source"]

    monkeypatch.setattr(fork_codex, "warm_tool_boundary_prefix_bytes", lambda *_a: 4096)
    monkeypatch.setattr(fork_render, "capture_warm_fork_source", capture)
    monkeypatch.setattr(harness_checkpoint_resume, "_prepare_external_checkpoint", prepare)
    _LIVE.put(thread_id, live)
    try:
        async with _LIVE.lock(thread_id):
            result = await asyncio.wait_for(
                harness_checkpoint_resume._prepare_initial_external_checkpoint(
                    "checkpoint", False, context=context, live=live,
                    thread_id=thread_id, identity=object(),
                    prepared=SimpleNamespace(mode="provider_resume"),
                ), 0.2,
            )
        assert result is source
    finally:
        _LIVE.pop(thread_id, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "reason"),
    [("replacement", "live_changed"), ("tool", "tool_calls_outstanding")],
)
async def test_warm_capture_discards_source_if_handle_changes_during_io(
    monkeypatch, change, reason,
):
    from loguru import logger

    from core import fork_codex, fork_render
    from subagents.codex_runtime_state import _LIVE

    thread_id = "changing-warm"
    live = SimpleNamespace(tool_step_ids={})
    context = SimpleNamespace(
        target=SimpleNamespace(harness="codex"), boundary=object(),
        warm_boundary=object(), binary="codex", reasoning_effort="high",
    )

    async def capture(*_args, **_kwargs):
        if change == "replacement":
            _LIVE.put(thread_id, SimpleNamespace(tool_step_ids={}))
        else:
            live.tool_step_ids["pending"] = "step"
        return object()

    monkeypatch.setattr(fork_codex, "warm_tool_boundary_prefix_bytes", lambda *_a: 4096)
    monkeypatch.setattr(fork_render, "capture_warm_fork_source", capture)
    refusals: list[str] = []
    sink = logger.add(
        refusals.append, level="INFO", format="{message}",
        filter=lambda record: "warm capture refused" in record["message"],
    )
    _LIVE.put(thread_id, live)
    try:
        assert await fork_codex._capture_tool_boundary_warm_source(
            context, live, thread_id,
        ) is None
        assert [line.strip() for line in refusals] == [
            f"fork warm capture refused for {thread_id} (codex): {reason}"
        ]
    finally:
        logger.remove(sink)
        _LIVE.pop(thread_id, None)


@pytest.mark.asyncio
async def test_shutdown_cancels_all_recovery_children_without_duplicate_wakes(monkeypatch):
    from automation import restart_startup
    from infra import thread_execution

    started, cancelled = set(), set()
    all_started = asyncio.Event()

    async def drive(batch):
        started.add(batch.thread_id)
        if len(started) == 2:
            all_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.add(batch.thread_id)

    async def no_work(*_args):
        return None

    async def duplicate_wake(_thread_id):
        pytest.fail("already-claimed wake must not drive the same conversation twice")

    monkeypatch.setattr(thread_execution, "drive_claimed_batch", drive)
    monkeypatch.setattr(thread_execution, "drive_thread", duplicate_wake)
    monkeypatch.setattr(restart_startup, "_replay_held_webhooks", no_work)
    recovery = SimpleNamespace(
        settlements=(),
        batches=tuple(SimpleNamespace(thread_id=t, turn_id=t) for t in ("cc", "native")),
    )
    task = asyncio.create_task(restart_startup.drive_startup_continuity(
        recovery, ("cc", "native"), logger,
    ))
    try:
        await asyncio.wait_for(all_started.wait(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cancelled == started == {"cc", "native"}


@pytest.mark.asyncio
async def test_independent_recovery_cancellation_is_not_success(monkeypatch):
    from automation import restart_startup
    from infra import thread_execution

    completed = []
    errors = []

    async def drive(batch):
        if batch.thread_id == "cancelled":
            raise asyncio.CancelledError("user stopped this conversation")
        completed.append(batch.thread_id)

    async def no_work(*_args):
        return None

    monkeypatch.setattr(thread_execution, "drive_claimed_batch", drive)
    monkeypatch.setattr(restart_startup, "_replay_held_webhooks", no_work)
    recovery = SimpleNamespace(
        settlements=(),
        batches=tuple(SimpleNamespace(thread_id=t, turn_id=t) for t in ("cancelled", "sibling")),
    )
    log = SimpleNamespace(info=lambda *_a: None, error=lambda *a: errors.append(a))
    with pytest.raises(BaseExceptionGroup) as caught:
        await restart_startup.run_startup_drive(no_work(), recovery, (), log)
    assert len(caught.value.exceptions) == 1
    assert isinstance(caught.value.exceptions[0], asyncio.CancelledError)
    assert completed == ["sibling"]
    assert len(errors) == 1 and "drive failed" in errors[0][0]
