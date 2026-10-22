"""Lifecycle contracts for the one checkpoint runtime."""

from __future__ import annotations

from core.checkpoint_types import CheckpointProviderForkRequest

import asyncio
import json
from dataclasses import replace

import pytest
from core import fork_runtime

from core.checkpoint_fork_kind import CHECKPOINT_KIND
from core.fork_types import (
    ProviderForkRequest,
    ProviderForkResult,
)
from core.checkpoint_types import CheckpointRequest
from llm.resolution import ExecutionTarget


def _target() -> ExecutionTarget:
    return ExecutionTarget(
        catalog_id="openai/test-checkpoint",
        harness="native",
        provider="openai",
        backend="api",
        transport_model="test-checkpoint",
        expected_reported_model=None,
        context_window=200_000,
        available=True,
        availability_reason=None,
        display_name="test",
        supports_reasoning=True,
        preserve_thinking_default=True,
    )


def _seed(thread_id: str) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads "
            "(id,user_id,model,history_epoch,created_at,updated_at,next_message_seq) "
            "VALUES (?, 'user-1', 'openai/test-checkpoint', 0, 't0', 't0', 4)",
            (thread_id,),
        )
        for seq in range(1, 4):
            db.execute(
                "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
                "VALUES (?,?,?,'body','t0',?)",
                (
                    f"{thread_id}-m{seq}",
                    thread_id,
                    "user" if seq == 1 else "assistant",
                    seq,
                ),
            )
        db.commit()
    finally:
        db.close()


def _checkpoint(thread_id: str, checkpoint_id: str) -> CheckpointRequest:
    return CheckpointRequest(
        id=checkpoint_id,
        thread_id=thread_id,
        history_epoch=0,
        previous_generation_id=None,
        through_seq=3,
        through_message_id=f"{thread_id}-m3",
        trigger="pressure",
        mode="warm",
        source_harness="native",
        source_model="openai/test-checkpoint",
        writer_harness="native",
        writer_model="openai/test-checkpoint",
        originating_turn_id="turn-1",
        first_included_seq=1,
        started_at="t1",
    )


def _provider(thread_id: str, checkpoint_id: str) -> ProviderForkRequest:
    return CheckpointProviderForkRequest(
        fork_id=checkpoint_id,
        thread_id=thread_id,
        target=_target(),
        instruction="Write one checkpoint.",
        reasoning_effort="high",
        working_dir="/tmp",
    )


@pytest.fixture(autouse=True)
async def _clean_runtime():
    from core import checkpoint_runtime

    await fork_runtime.shutdown_forks()
    await checkpoint_runtime.shutdown_checkpoint_wakes()
    yield
    await fork_runtime.shutdown_forks()
    await checkpoint_runtime.shutdown_checkpoint_wakes()


@pytest.mark.asyncio
async def test_active_checkpoint_coalesces_without_another_admission(
    isolated_db, monkeypatch
):
    from core import checkpoint_runtime as runtime

    _seed("thread-1")
    release = asyncio.Event()
    calls = 0

    async def invoke(_request, cancel):
        nonlocal calls
        calls += 1
        await _wait_for(release, cancel)
        return ProviderForkResult("body", None)

    monkeypatch.setattr(runtime, "_invoke", invoke)
    first = await runtime.request_checkpoint(
        _checkpoint("thread-1", "cp-1"), _provider("thread-1", "cp-1")
    )
    joined = await runtime.request_checkpoint(_checkpoint("thread-1", "cp-2"), None)

    assert (first.id, joined.id, calls) == ("cp-1", "cp-1", 1)
    release.set()
    assert (await runtime.await_checkpoint("cp-1")).status == "installed"


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["pressure", "manual"])
async def test_non_admission_creates_no_row_or_queued_work(isolated_db, trigger):
    from core import checkpoint_runtime as runtime
    from infra.database import get_db

    _seed("declined")
    request = replace(_checkpoint("declined", "cp-declined"), trigger=trigger)

    with pytest.raises(runtime.ForkNotAdmitted):
        await runtime.request_checkpoint(request, None)

    db = get_db()
    try:
        assert db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
    finally:
        db.close()
    assert fork_runtime.fork_task_count() == 0


@pytest.mark.asyncio
async def test_result_is_captured_before_install_and_then_wakes_thread(
    isolated_db, monkeypatch
):
    from core import checkpoint_runtime as runtime
    from infra import checkpoint_queries, checkpoints as store

    _seed("thread-1")
    observed = []
    real_install = store.install

    async def invoke(_request, _cancel):
        return ProviderForkResult(
            "paid body",
            {"input_tokens": 101},
            diagnostic="provider process exited after usable output",
        )

    def install(checkpoint_id, **kwargs):
        observed.append(("before_install", checkpoint_queries.read(checkpoint_id).status))
        return real_install(checkpoint_id, **kwargs)

    async def notify(record):
        observed.append(("notify", record.status))

    monkeypatch.setattr(runtime, "_invoke", invoke)
    monkeypatch.setattr(store, "install", install)
    monkeypatch.setattr(runtime, "_notify_completion", notify)

    await runtime.request_checkpoint(
        _checkpoint("thread-1", "cp-1"), _provider("thread-1", "cp-1")
    )
    record = await runtime.await_checkpoint("cp-1")

    assert record.status == "installed"
    assert record.raw_result == "paid body"
    assert json.loads(record.usage_json) == {"calls": [{"input_tokens": 101}]}
    assert record.error == "provider process exited after usable output"
    assert observed == [("before_install", "captured"), ("notify", "installed")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "mode", "provider_usage", "expected_usage"),
    [
        (
            "native",
            "warm",
            {"calls": [{"prompt_tokens": 100}, {"prompt_tokens": 120}]},
            {"calls": [{"prompt_tokens": 100}, {"prompt_tokens": 120}]},
        ),
        (
            "native",
            "cold",
            {"calls": [{"prompt_tokens": 100}]},
            {"calls": [{"prompt_tokens": 100}]},
        ),
        (
            "claude-code",
            "warm",
            {"input_tokens": 80, "output_tokens": 20},
            {"calls": [{"input_tokens": 80, "output_tokens": 20}]},
        ),
        (
            "claude-code",
            "cold",
            {"input_tokens": 81, "output_tokens": 21},
            {"calls": [{"input_tokens": 81, "output_tokens": 21}]},
        ),
        (
            "codex",
            "warm",
            {"input_tokens": 300, "cached_input_tokens": 200, "output_tokens": 40},
            {
                "calls": [
                    {
                        "input_tokens": 300,
                        "cached_input_tokens": 200,
                        "output_tokens": 40,
                    }
                ]
            },
        ),
        (
            "codex",
            "cold",
            {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 40},
            {
                "calls": [
                    {
                        "input_tokens": 100,
                        "cached_input_tokens": 0,
                        "output_tokens": 40,
                    }
                ]
            },
        ),
    ],
)
async def test_checkpoint_persists_one_per_call_usage_shape_for_all_harnesses_and_modes(
    isolated_db, monkeypatch, harness, mode, provider_usage, expected_usage,
):
    """A durable checkpoint never exposes a flat session-total usage map."""
    from core import checkpoint_runtime as runtime

    _seed("thread-1")
    request = replace(
        _checkpoint("thread-1", "cp-1"),
        mode=mode,
        source_harness=harness,
        writer_harness=harness,
    )
    provider = replace(
        _provider("thread-1", "cp-1"),
        target=replace(_target(), harness=harness),
    )

    async def invoke(_request, _cancel):
        return ProviderForkResult("paid body", provider_usage)

    monkeypatch.setattr(runtime, "_invoke", invoke)

    await runtime.request_checkpoint(request, provider)
    record = await runtime.await_checkpoint("cp-1")

    assert record.status == "installed"
    assert json.loads(record.usage_json) == expected_usage


@pytest.mark.asyncio
async def test_install_appends_and_broadcasts_one_completion_receipt(
    isolated_db, monkeypatch
):
    from core import checkpoint_runtime as runtime
    from infra import send_broadcast, thread_execution
    from infra.database import get_db

    _seed("thread-1")
    broadcasts = []

    async def invoke(_request, _cancel):
        return ProviderForkResult("paid body", None)

    async def broadcast(thread_id, message_ids):
        broadcasts.append((thread_id, message_ids))

    async def redrive(_thread_id):
        return None

    monkeypatch.setattr(runtime, "_invoke", invoke)
    monkeypatch.setattr(send_broadcast, "broadcast_message_rows", broadcast)
    monkeypatch.setattr(thread_execution, "drive_thread", redrive)

    await runtime.request_checkpoint(
        _checkpoint("thread-1", "cp-1"), _provider("thread-1", "cp-1")
    )
    assert (await runtime.await_checkpoint("cp-1")).status == "installed"

    db = get_db()
    try:
        row = db.execute(
            "SELECT content,metadata,seq FROM messages WHERE id=?",
            ("checkpoint-receipt:cp-1",),
        ).fetchone()
    finally:
        db.close()

    assert row is not None
    assert (row["content"], row["seq"]) == ("Context saved", 4)
    assert json.loads(row["metadata"]) == {
        "type": "activity_receipt",
        "event_type": "checkpoint.installed",
        "receipt_type": "checkpoint",
        "checkpoint_id": "cp-1",
        "checkpoint_mode": "warm",
        "checkpoint_trigger": "pressure",
        "through_seq": 3,
        "summary": "Context saved",
        "resume_presence_line": None,
        "responsibility_map_snapshot": {
            "model": "openai/test-checkpoint",
            "execution_epoch": 0,
            "project_id": None,
            "runtime_context_epoch": 0,
            "content": (
                "[Agent responsibility map]\n"
                "Self: this thread (thread-1) has no registered responsibility.\n"
                "Use agent_roster for paths, domains, and coordination notes."
            ),
            "agent_count": 0,
            "agent_thread_ids": [],
        },
    }
    assert broadcasts == [("thread-1", ("checkpoint-receipt:cp-1",))]


@pytest.mark.asyncio
async def test_checkpoint_completion_does_not_wait_for_thread_redrive(
    isolated_db, monkeypatch
):
    from core import checkpoint_runtime as runtime
    from infra import thread_execution

    _seed("thread-1")
    wake_entered = asyncio.Event()
    release_wake = asyncio.Event()
    wake_done = asyncio.Event()

    async def invoke(_request, _cancel):
        return ProviderForkResult("paid body", None)

    async def blocked_redrive(_thread_id):
        wake_entered.set()
        try:
            await release_wake.wait()
        finally:
            wake_done.set()

    monkeypatch.setattr(runtime, "_invoke", invoke)
    monkeypatch.setattr(thread_execution, "drive_thread", blocked_redrive)

    await runtime.request_checkpoint(
        _checkpoint("thread-1", "cp-1"), _provider("thread-1", "cp-1")
    )
    waiter = asyncio.create_task(runtime.await_checkpoint("cp-1"))
    try:
        await asyncio.wait_for(wake_entered.wait(), timeout=1)
        await asyncio.sleep(0)
        assert waiter.done()
        assert (await waiter).status == "installed"
    finally:
        release_wake.set()
        if not waiter.done():
            await waiter
        await asyncio.wait_for(wake_done.wait(), timeout=1)


@pytest.mark.asyncio
async def test_cancel_commits_before_provider_exit(isolated_db, monkeypatch):
    from core import checkpoint_runtime as runtime

    _seed("thread-1")
    started = asyncio.Event()
    exited = asyncio.Event()
    allow_exit = asyncio.Event()

    async def invoke(_request, cancel):
        started.set()
        try:
            await cancel.wait()
            raise asyncio.CancelledError
        finally:
            await allow_exit.wait()
            exited.set()

    monkeypatch.setattr(runtime, "_invoke", invoke)
    await runtime.request_checkpoint(
        _checkpoint("thread-1", "cp-1"), _provider("thread-1", "cp-1")
    )
    await started.wait()

    record = await runtime.cancel_checkpoint("cp-1")

    assert record.status == "cancelled"
    assert not exited.is_set()
    allow_exit.set()
    assert (await runtime.await_checkpoint("cp-1")).status == "cancelled"
    assert exited.is_set()


@pytest.mark.asyncio
async def test_cancel_before_task_dispatch_never_starts_provider(
    isolated_db, monkeypatch
):
    from core import checkpoint_runtime as runtime

    _seed("thread-1")
    calls = 0

    async def invoke(_request, _cancel):
        nonlocal calls
        calls += 1
        return ProviderForkResult("should not run", None)

    monkeypatch.setattr(runtime, "_invoke", invoke)
    await runtime.request_checkpoint(
        _checkpoint("thread-1", "cp-1"), _provider("thread-1", "cp-1")
    )
    record = await runtime.cancel_checkpoint("cp-1")

    assert record.status == "cancelled"
    assert (await runtime.await_checkpoint("cp-1")).status == "cancelled"
    assert calls == 0


@pytest.mark.asyncio
async def test_cancelling_a_joiner_detaches_from_independent_checkpoint(
    isolated_db, monkeypatch
):
    from core import checkpoint_runtime as runtime

    _seed("thread-1")
    release = asyncio.Event()

    async def invoke(_request, cancel):
        await _wait_for(release, cancel)
        return ProviderForkResult("body", None)

    monkeypatch.setattr(runtime, "_invoke", invoke)
    await runtime.request_checkpoint(
        _checkpoint("thread-1", "cp-1"), _provider("thread-1", "cp-1")
    )
    joiner = asyncio.create_task(runtime.await_checkpoint("cp-1"))
    await asyncio.sleep(0)
    joiner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await joiner

    assert fork_runtime.fork_task_count() == 1
    assert (await runtime._read("cp-1")).status == "running"
    release.set()
    assert (await runtime.await_checkpoint("cp-1")).status == "installed"


@pytest.mark.asyncio
async def test_provider_failure_is_one_terminal_row(isolated_db, monkeypatch):
    from core import checkpoint_runtime as runtime

    _seed("thread-1")

    async def reject(_request, _cancel):
        raise RuntimeError("quota refused")

    monkeypatch.setattr(runtime, "_invoke", reject)
    await runtime.request_checkpoint(
        _checkpoint("thread-1", "cp-1"), _provider("thread-1", "cp-1")
    )

    record = await runtime.await_checkpoint("cp-1")
    assert (record.status, record.error) == ("failed", "quota refused")


@pytest.mark.asyncio
async def test_task_exit_before_capture_is_failed(isolated_db, monkeypatch):
    from core import checkpoint_runtime as runtime

    _seed("thread-1")

    async def invoke(_request, _cancel):
        return ProviderForkResult("body", None)

    async def capture_failure(_checkpoint_id, _result):
        raise RuntimeError("capture persistence failed")

    monkeypatch.setattr(runtime, "_invoke", invoke)
    monkeypatch.setattr(runtime, "CHECKPOINT_KIND", replace(CHECKPOINT_KIND, end_handler=capture_failure))
    await runtime.request_checkpoint(
        _checkpoint("thread-1", "cp-1"), _provider("thread-1", "cp-1")
    )

    record = await runtime.await_checkpoint("cp-1")

    assert (record.status, record.error) == ("failed", "capture persistence failed")


@pytest.mark.asyncio
async def test_install_failure_ends_the_attempt_failed_without_retry(
    isolated_db, monkeypatch,
):
    from core import checkpoint_runtime as runtime
    from infra import checkpoints as store

    _seed("thread-1")
    real_install = store.install
    installs: list[str] = []

    async def invoke(_request, _cancel):
        return ProviderForkResult("body", None)

    def install(checkpoint_id, **kwargs):
        installs.append(checkpoint_id)
        if checkpoint_id == "cp-1":
            raise RuntimeError("install persistence failed")
        return real_install(checkpoint_id, **kwargs)

    monkeypatch.setattr(runtime, "_invoke", invoke)
    monkeypatch.setattr(store, "install", install)
    await runtime.request_checkpoint(
        _checkpoint("thread-1", "cp-1"), _provider("thread-1", "cp-1")
    )

    record = await runtime.await_checkpoint("cp-1")

    assert (record.status, record.error) == ("failed", "install persistence failed")
    assert record.raw_result == "body"
    assert store.reconcile_startup(reconciled_at="t9") == ()
    fresh = await runtime.request_checkpoint(
        _checkpoint("thread-1", "cp-fresh"), _provider("thread-1", "cp-fresh")
    )
    assert fresh.id == "cp-fresh"
    assert (await runtime.await_checkpoint("cp-fresh")).status == "installed"
    assert installs == ["cp-1", "cp-fresh"]


@pytest.mark.asyncio
async def test_blank_transport_fails_even_with_staged_edits(
    isolated_db, monkeypatch
):
    from core import checkpoint_runtime as runtime
    from core.fork_types import ProviderForkRejected

    _seed("thread-1")

    async def no_body(_request, _cancel):
        from infra.database import get_db
        from infra.continuity_staging import stage_edit

        db = get_db()
        try:
            stage_edit(db, checkpoint_id="cp-1", thread_id="thread-1", operation="add", params={
                "field": "next_actions", "body": {"statement": "Retain this edit.", "source_refs": []},
            })
            db.commit()
        finally:
            db.close()
        raise ProviderForkRejected("empty envelope")

    monkeypatch.setattr(runtime, "_invoke", no_body)
    await runtime.request_checkpoint(
        _checkpoint("thread-1", "cp-1"), _provider("thread-1", "cp-1")
    )

    record = await runtime.await_checkpoint("cp-1")
    # A blank narrative must never install (#1767): once installed it becomes
    # the thread's whole reconstruction source, so a blank one would silently
    # discard the history it claims to cover. Staged edits alone can't earn it.
    assert record.status == "failed"
    assert record.installed_at is None

    from infra.continuity_store import active_entry_snapshot
    from infra.database import get_db

    db = get_db()
    try:
        # The staged edit must not be committed as if the checkpoint installed.
        assert active_entry_snapshot(db, "thread-1", 0) == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_truncated_cutoff_with_no_new_text_fails_despite_prior_narrative(
    isolated_db, monkeypatch
):
    """A blank truncated body is a failed attempt regardless of a nonblank
    previous generation: gating the raw provider body before carry-forward
    means this call never silently re-stamps the old narrative (#1767)."""
    from core import checkpoint_runtime as runtime
    from infra.database import get_db

    _seed("thread-1")

    async def first_invoke(_request, _cancel):
        return ProviderForkResult("first narrative", None)

    monkeypatch.setattr(runtime, "_invoke", first_invoke)
    await runtime.request_checkpoint(
        _checkpoint("thread-1", "cp-1"), _provider("thread-1", "cp-1")
    )
    first = await runtime.await_checkpoint("cp-1")
    assert first.status == "installed"

    db = get_db()
    try:
        # cp-1's install already consumed the next seq for its own completion
        # receipt, so pick a fresh one instead of assuming seq 4 is free.
        next_seq = int(db.execute(
            "SELECT next_message_seq FROM threads WHERE id='thread-1'"
        ).fetchone()[0])
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
            "VALUES ('thread-1-m-next','thread-1','assistant','body','t0',?)",
            (next_seq,),
        )
        db.commit()
    finally:
        db.close()

    async def second_invoke(_request, _cancel):
        return ProviderForkResult("", None, "checkpoint_context_truncated")

    monkeypatch.setattr(runtime, "_invoke", second_invoke)
    second_request = replace(
        _checkpoint("thread-1", "cp-2"),
        previous_generation_id="cp-1", through_seq=next_seq,
        through_message_id="thread-1-m-next",
    )
    await runtime.request_checkpoint(second_request, _provider("thread-1", "cp-2"))
    second = await runtime.await_checkpoint("cp-2")

    assert second.status == "failed"
    assert second.installed_at is None
    # The previous installed generation is untouched and stays the head.
    from infra import checkpoint_queries

    assert checkpoint_queries.current_installed("thread-1").id == "cp-1"


@pytest.mark.asyncio
async def test_cold_invoke_adds_frozen_continuity_index_after_creation(
    isolated_db, monkeypatch,
):
    from core import checkpoint_runtime as runtime
    from infra.checkpoints import create_running
    from infra.continuity_store import add_entry
    from infra.database import get_db
    from native_checkpoint_fixtures import cold_source

    _seed("thread-1")
    db = get_db()
    try:
        entry_id = add_entry(
            db, thread_id="thread-1", history_epoch=0, field="facts",
            body={"statement": "SECRET BODY", "source_refs": []},
        )
        create_running(_checkpoint("thread-1", "cp-1"), db=db)
        db.commit()
    finally:
        db.close()
    captured = {}

    async def invoke(request, _cancel, **_kwargs):
        captured["instruction"] = request.instruction
        return ProviderForkResult("body", None)

    monkeypatch.setattr("core.fork_native.invoke_fork", invoke)
    request = replace(
        _provider("thread-1", "cp-1"),
        cold_source=cold_source(thread_id="thread-1"),
    )

    await runtime._invoke(request, asyncio.Event())

    instruction = captured["instruction"]
    assert json.dumps({"facts": [entry_id]}, separators=(",", ":")) in instruction
    # The frozen index names no body; only the review below it offers one.
    assert "SECRET BODY" not in instruction.split("[CORSIA saved-entry review]", 1)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "adapter"),
    [
        ("native", "core.fork_native"),
        ("claude-code", "core.fork_cc"),
        ("codex", "core.fork_codex"),
    ],
)
async def test_placement_is_decided_by_the_fork_on_the_threads_model_and_effort(
    isolated_db, monkeypatch, harness, adapter,
):
    """Lesson placement rides the checkpoint fork itself: no other model call."""
    from core import checkpoint_runtime as runtime
    from infra.checkpoints import create_running
    from infra.continuity_store import add_entry
    from infra.database import get_db

    _seed("thread-1")
    db = get_db()
    try:
        entry_id = add_entry(
            db, thread_id="thread-1", history_epoch=0, field="decisions",
            body={"statement": "prefer small commits", "rationale": "r",
                  "source_refs": [{"kind": "user", "reference": "chat"}]},
        )
        create_running(_checkpoint("thread-1", "cp-1"), db=db)
        db.commit()
    finally:
        db.close()
    captured = {}

    async def invoke(request, _cancel, **_kwargs):
        captured["request"] = request
        return ProviderForkResult("body", None)

    monkeypatch.setattr(f"{adapter}.invoke_fork", invoke)
    monkeypatch.setattr(
        "core.placement_answer.history", lambda db, thread_id, entry: [],
    )
    target = replace(_target(), harness=harness)
    request = replace(_provider("thread-1", "cp-1"), target=target)

    await runtime._invoke(request, asyncio.Event())

    sent = captured["request"]
    assert (sent.target, sent.reasoning_effort) == (target, "high")
    assert sent.instruction.startswith("Write one checkpoint.")
    review = sent.instruction.split("[CORSIA saved-entry review]", 1)[1]
    offered = json.loads(review.split("Entries:\n", 1)[1])
    assert [lesson["entry_id"] for lesson in offered] == [entry_id]


@pytest.mark.asyncio
async def test_shutdown_cancels_and_joins_every_exact_checkpoint(
    isolated_db, monkeypatch
):
    from core import checkpoint_runtime as runtime

    for thread_id in ("one", "two"):
        _seed(thread_id)

    async def invoke(_request, cancel):
        await cancel.wait()
        raise asyncio.CancelledError

    monkeypatch.setattr(runtime, "_invoke", invoke)
    for thread_id in ("one", "two"):
        await runtime.request_checkpoint(
            _checkpoint(thread_id, f"cp-{thread_id}"),
            _provider(thread_id, f"cp-{thread_id}"),
        )

    await fork_runtime.shutdown_forks()
    await runtime.shutdown_checkpoint_wakes()

    assert fork_runtime.fork_task_count() == 0
    assert (await runtime.await_checkpoint("cp-one")).status == "cancelled"
    assert (await runtime.await_checkpoint("cp-two")).status == "cancelled"


@pytest.mark.asyncio
async def test_startup_fails_running_and_installs_captured(isolated_db, monkeypatch):
    from core import checkpoint_runtime as runtime
    from infra import checkpoints as store

    for thread_id in ("running", "captured"):
        _seed(thread_id)
        store.create_running(_checkpoint(thread_id, f"cp-{thread_id}"))
    store.capture("cp-captured", raw_result="saved", usage_json=None, captured_at="t2")
    notified = []

    async def notify(record):
        notified.append((record.thread_id, record.status))

    monkeypatch.setattr(runtime, "_notify_completion", notify)
    records = await runtime.reconcile_startup_checkpoints()

    assert {row.id: row.status for row in records} == {
        "cp-running": "failed",
        "cp-captured": "installed",
    }
    assert notified == [("running", "failed"), ("captured", "installed")]


async def _wait_for(release: asyncio.Event, cancel: asyncio.Event) -> None:
    released = asyncio.create_task(release.wait())
    stopped = asyncio.create_task(cancel.wait())
    try:
        await asyncio.wait({released, stopped}, return_when=asyncio.FIRST_COMPLETED)
        if cancel.is_set():
            raise asyncio.CancelledError
    finally:
        for task in (released, stopped):
            task.cancel()
        await asyncio.gather(released, stopped, return_exceptions=True)
