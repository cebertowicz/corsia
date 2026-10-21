"""#1921: Stop ends the required checkpoint its Turn is blocked on.

The tool-boundary decision, checkpoint admission, runtime, and storage are the
real ones on an isolated database. Only the provider invocation is synthetic,
plus the transport swap after an install, which the hold/install tests cover.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from codex_runtime_fixtures import declare_codex_tool_step
from core.fork_types import ProviderForkResult
from core.checkpoint_types import CheckpointUnavailable
from core.checkpoint_wait import CheckpointWaitScope
from core.turn_core import TurnIdentity
from test_checkpoint_hold_install_resume import _Process, _reach_wait, _seed

WINDOW = 32_768


def _next_turn(db, live, turn_id: str, order: int) -> None:
    """End the previous Turn and bind the next one, as a new message would."""
    db.execute(
        "UPDATE active_turns SET status='cancelled',ended_at='t' "
        "WHERE thread_id='held-thread' AND status='running'"
    )
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,turn_order,model,"
        "status,started_at,ended_at,boot_epoch) "
        "VALUES (?,'held-thread',0,?,?,'running','t',NULL,'boot')",
        (turn_id, order, live.catalog_model_id),
    )
    db.commit()
    live.corsia_turn = TurnIdentity(turn_id, 0)
    live.checkpoint_wait_scope = CheckpointWaitScope()


def _claude_live(monkeypatch, tmp_path, target):
    from registry.service import register_cc_session
    from subagents.cc_runtime_state import _LIVE, _Live, activate_bound_turn
    from subagents.cc_stream import CCStreamSession

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    session = CCStreamSession(binary="unused", session_id="old-provider",
        working_dir=str(tmp_path), claude_dir=str(tmp_path / "claude"))
    session._proc = _Process()
    live = _Live(session=session, parent_thread_id=None, name="old",
        catalog_model_id=target.catalog_id, context_window=target.context_window,
        corsia_turn=TurnIdentity("turn-current", 0),
        last_call_usage={"input_tokens": WINDOW - 8, "output_tokens": 8})
    assert activate_bound_turn(live, live.corsia_turn)
    _LIVE.put("held-thread", live)
    return live, register_cc_session, _LIVE.reset


def _codex_live(monkeypatch, tmp_path, target):
    from registry.service import register_codex_thread
    from subagents.codex_app_server import CodexAppServerSession
    from subagents.codex_runtime_state import _APP_SESSIONS, _CODEX_THREADS, _LIVE, _LiveCodex

    session = CodexAppServerSession(binary="unused", task=None, working_dir=str(tmp_path))
    session._proc = _Process()
    session.thread_id = "old-provider"
    live = _LiveCodex(session=session, parent_thread_id=None, name="old",
        catalog_model_id=target.catalog_id, context_window=target.context_window,
        corsia_turn=TurnIdentity("turn-current", 0), codex_thread_id="old-provider")
    _LIVE.put("held-thread", live)
    _APP_SESSIONS["held-thread"] = session
    _CODEX_THREADS["held-thread"] = live.codex_thread_id

    def reset() -> None:
        _LIVE.reset()
        _APP_SESSIONS.clear()
        _CODEX_THREADS.clear()

    return live, register_codex_thread, reset


def _statuses(db) -> dict[str, str]:
    return {
        row["id"]: row["status"]
        for row in db.execute(
            "SELECT id,status FROM checkpoints WHERE thread_id='held-thread' "
            "AND id<>'floor' ORDER BY generation_order"
        )
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ("claude-code", "codex"))
async def test_stop_ends_the_awaited_attempt_and_the_next_turn_proceeds(
    isolated_db, monkeypatch, tmp_path, harness,
):
    from core import checkpoint_runtime as runtime
    from infra.database import get_db
    from llm.resolution import resolve_execution_target
    from subagents.harness_checkpoint_resume import maintain_external_tool_boundary
    from tests.cc_runtime_fixtures import cc_target

    base = cc_target() if harness == "claude-code" else resolve_execution_target("codex/gpt-5-codex")
    target = replace(base, context_window=WINDOW)
    monkeypatch.setattr("llm.resolution.resolve_execution_target", lambda *_a, **_k: target)
    monkeypatch.setattr("subagents.codex_runtime_support.resolve_working_dir", lambda *_: str(tmp_path))
    build = _claude_live if harness == "claude-code" else _codex_live
    live, register, reset = build(monkeypatch, tmp_path, target)
    db = get_db()
    _seed(db, target.catalog_id)
    register("held-thread", "old-provider", checkpoint_generation_id="floor", db=db)
    db.commit()

    invocations: list[str] = []

    async def invoke(request, cancel):
        invocations.append(request.fork_id)
        if len(invocations) == 1:
            raise RuntimeError("provider refused the fold")
        if len(invocations) == 2:
            # A stalled writer: neither finishes nor fails; only cancel ends it.
            await cancel.wait()
            raise asyncio.CancelledError
        return ProviderForkResult("FRESH_CHECKPOINT_BODY", None)

    waiting = asyncio.Event()
    await_original = runtime.await_checkpoint

    async def observe_wait(checkpoint_id):
        waiting.set()
        return await await_original(checkpoint_id)

    resumed: list[object] = []

    async def resume_external(_live, _thread_id, payload, _harness):
        resumed.append(payload)

    monkeypatch.setattr(runtime, "_invoke", invoke)
    monkeypatch.setattr(runtime, "await_checkpoint", observe_wait)
    monkeypatch.setattr(runtime, "_notify_completion", AsyncMock())
    monkeypatch.setattr(
        "subagents.harness_checkpoint_resume._resume_external", resume_external,
    )

    def tool_boundary():
        # One settled tool result just crossed the hard wall.
        return asyncio.create_task(maintain_external_tool_boundary(
            live, "held-thread", harness, complete_input_tokens=WINDOW + 20,
        ))

    tasks: list[asyncio.Task] = []
    try:
        # Turn 1: the required checkpoint fails and the Turn ends visibly.
        tasks.append(tool_boundary())
        with pytest.raises(CheckpointUnavailable, match="provider refused the fold"):
            await asyncio.wait_for(tasks[-1], 5)
        failed = invocations[0]

        # Turn 2: the next boundary starts a second required attempt and blocks.
        _next_turn(db, live, "turn-2", 3)
        waiting.clear()
        tasks.append(tool_boundary())
        await _reach_wait(tasks[-1], waiting)
        stalled = live.checkpoint_wait_scope.checkpoint_id
        assert stalled is not None and stalled != failed
        assert _statuses(db) == {failed: "failed", stalled: "running"}

        # Stop ends the Turn and the attempt it was blocked on, durably.
        live.checkpoint_wait_scope.request_stop()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(tasks[-1], 5)
        assert _statuses(db) == {failed: "failed", stalled: "cancelled"}
        assert live.checkpoint_wait_scope.checkpoint_id is None

        # Turn 3, the next message: it does not rejoin the stopped attempt. A
        # fresh attempt installs and the boundary proceeds on it.
        _next_turn(db, live, "turn-3", 4)
        tasks.append(tool_boundary())
        assert await asyncio.wait_for(tasks[-1], 5) is True
        fresh = invocations[2]
        assert invocations == [failed, stalled, fresh]
        assert _statuses(db) == {failed: "failed", stalled: "cancelled", fresh: "installed"}
        assert len(resumed) == 1
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.shutdown_fork_tasks()
        await live.session.terminate()
        reset()
        db.close()


REASON = "the minimum provider continuation exceeds the context window"


def _refusal() -> CheckpointUnavailable:
    return CheckpointUnavailable(
        REASON, context_window=WINDOW, installed_checkpoint_id="floor",
    )


@pytest.mark.asyncio
async def test_claude_code_failure_carries_the_checkpoint_reason(monkeypatch) -> None:
    from types import SimpleNamespace

    from subagents import harness_tool_bridge
    from tools.recorded_call import CompletedToolFailure, combine_completed_failures

    async def refuse(*_args, **_kwargs):
        raise _refusal()

    monkeypatch.setattr(harness_tool_bridge, "_settle_cc_tool_result", refuse)
    delivery = SimpleNamespace(
        name="read_file", content="full result", invocation_id="call-1",
        provider_tool_call_id="call-1", assistant_step_id="step-1",
    )
    result = SimpleNamespace(completion=None, delivery=delivery)

    with pytest.raises(CompletedToolFailure) as failure:
        await harness_tool_bridge.settle_cc_tool_result(
            SimpleNamespace(), "thread-1", result, TurnIdentity("turn-1", 0),
        )

    assert str(failure.value) == REASON
    assert failure.value.completed[0].result == "full result"
    # The terminal latch that ends the Turn keeps the same reason.
    assert str(combine_completed_failures(None, failure.value)) == REASON


@pytest.mark.asyncio
async def test_codex_failure_carries_the_checkpoint_reason(monkeypatch) -> None:
    from types import SimpleNamespace

    from subagents.codex_app_server import CodexAppServerSession
    from tools.recorded_call import CompletedToolFailure, combine_completed_failures

    async def refuse(*_args, **_kwargs):
        raise _refusal()

    monkeypatch.setattr(
        "subagents.harness_tool_bridge.execute_tool", AsyncMock(return_value="full result"),
    )
    monkeypatch.setattr("subagents.harness_tool_bridge.settle_codex_tool_result", refuse)
    session = CodexAppServerSession(
        binary="codex", task=None, tool_registry=object(),
        corsia_thread_id="thread-1",
        corsia_turn_identity=SimpleNamespace(turn_id="turn-1"),
    )
    declare_codex_tool_step(session, "call-1", persist=False)

    with pytest.raises(CompletedToolFailure) as failure:
        await session._handle_dynamic_tool_request({
            "tool": "read_file", "arguments": {"path": "README.md"},
            "callId": "call-1",
        })

    assert str(failure.value) == REASON
    assert failure.value.completed[0].result == "full result"
    session.retain_completed_tool_failure(failure.value)
    assert str(session._terminal_error) == REASON
    assert str(combine_completed_failures(session._terminal_error, failure.value)) == REASON


POST_BINDING_CAUSE = "external checkpoint boundary changed before admission"


def test_combined_completed_failures_keep_the_first_reason_and_cause() -> None:
    """Accumulation adds effects without replacing the primary terminal failure."""
    from tools.recorded_call import (
        CompletedToolCall,
        CompletedToolFailure,
        combine_completed_failures,
    )

    first_cause = RuntimeError("first retained failure")
    later_cause = RuntimeError("later teardown artifact")
    first = CompletedToolFailure((CompletedToolCall("read_file", "first result"),), "first")
    later = CompletedToolFailure((CompletedToolCall("write_file", "later result"),), "later")
    first.__cause__, later.__cause__ = first_cause, later_cause

    combined = combine_completed_failures(first, later)

    assert str(combined) == "first"
    assert combined.__cause__ is first_cause
    assert [call.result for call in combined.completed] == ["first result", "later result"]


@pytest.mark.asyncio
async def test_claude_code_post_binding_failure_keeps_its_cause(monkeypatch, caplog) -> None:
    """#1921 follow-up: a non-checkpoint post-completion failure must stay attributable."""
    from types import SimpleNamespace

    from subagents import harness_tool_bridge
    from tools.recorded_call import CompletedToolFailure

    cause = RuntimeError(POST_BINDING_CAUSE)

    async def refuse(*_args, **_kwargs):
        raise cause

    monkeypatch.setattr(harness_tool_bridge, "_settle_cc_tool_result", refuse)
    delivery = SimpleNamespace(
        name="read_file", content="full result", invocation_id="call-1",
        provider_tool_call_id="call-1", assistant_step_id="step-1",
    )
    result = SimpleNamespace(completion=None, delivery=delivery)

    with caplog.at_level(logging.ERROR, logger="tools.recorded_call"):
        with pytest.raises(CompletedToolFailure) as failure:
            await harness_tool_bridge.settle_cc_tool_result(
                SimpleNamespace(), "thread-1", result, TurnIdentity("turn-1", 0),
            )

    error = failure.value
    assert error.__cause__ is cause
    assert error.completed[0].result == "full result"
    [record] = caplog.records
    assert record.exc_info is not None and record.exc_info[1] is cause
    message = record.getMessage()
    assert "claude-code" in message
    assert "thread-1" in message
    assert "turn-1" in message
    assert "read_file" in message
    assert "call-1" in message
    assert POST_BINDING_CAUSE in message
    assert "full result" not in message


@pytest.mark.asyncio
async def test_codex_post_binding_failure_logs_termination(monkeypatch, caplog) -> None:
    from types import SimpleNamespace

    from subagents.codex_app_server import CodexAppServerSession
    from subagents.codex_tool_requests import ToolRequests
    from tests.test_codex_tool_control_lifecycle import Process, tool_call
    from tools.recorded_call import CompletedToolFailure

    cause = RuntimeError(POST_BINDING_CAUSE)

    async def refuse(*_args, **_kwargs):
        raise cause

    async def usage(_self, _frame):
        return {
            "last": {"inputTokens": 100, "outputTokens": 10},
            "total": {"inputTokens": 100, "outputTokens": 10},
        }

    monkeypatch.setattr(ToolRequests, "_usage", usage)
    monkeypatch.setattr(
        "subagents.harness_tool_bridge.execute_tool", AsyncMock(return_value="full result"),
    )
    monkeypatch.setattr("subagents.harness_tool_bridge.settle_codex_tool_result", refuse)
    session = CodexAppServerSession(
        binary="codex", task=None, codex_thread_id="provider", tool_registry=object(),
        corsia_thread_id="thread-1",
        corsia_turn_identity=SimpleNamespace(turn_id="turn-1"),
    )
    session._proc = Process()
    session._track_active_turn("turn")
    declare_codex_tool_step(session, "call-1", persist=False)
    frame = tool_call()
    frame["params"].update({"tool": "read_file", "callId": "call-1"})

    with caplog.at_level(logging.ERROR):
        await session._tool_requests._execute(frame)

    error = session._terminal_error
    assert isinstance(error, CompletedToolFailure)
    assert error.__cause__ is cause
    assert error.completed[0].result == "full result"
    canonical = [r for r in caplog.records if "completed tool failure" in r.getMessage()]
    lifecycle = [r for r in caplog.records if "could not be published" in r.getMessage()]
    assert len(canonical) == len(lifecycle) == 1
    assert canonical[0].exc_info is not None and canonical[0].exc_info[1] is cause
    assert all(value in canonical[0].getMessage() for value in (
        "codex", "thread-1", "turn-1", "read_file", "call-1", POST_BINDING_CAUSE,
    ))
    assert all("full result" not in record.getMessage() for record in caplog.records)


def test_codex_reader_logs_cause_and_termination(caplog) -> None:
    """The unmanaged reader records both cause identity and process termination."""
    from types import SimpleNamespace

    from subagents.codex_app_server_reader import _handle_reader_crash
    from tools.recorded_call import CompletedToolCall, completed_tool_failure

    cause = RuntimeError(POST_BINDING_CAUSE)
    call = CompletedToolCall(
        name="read_file", result="full result", thread_id="thread-1",
        turn_id="turn-1", provider_tool_call_id="call-1",
    )
    session = SimpleNamespace(
        revoke_dynamic_tool_admission=lambda: None,
        retain_completed_tool_failure=lambda _error: None,
        _kill_process=lambda: None,
    )

    with caplog.at_level(logging.ERROR):
        failure = completed_tool_failure((call,), cause, harness="codex")
        _handle_reader_crash(session, failure, logging.getLogger(__name__))

    canonical = [r for r in caplog.records if "completed tool failure" in r.getMessage()]
    lifecycle = [r for r in caplog.records if "reader crashed; terminating" in r.getMessage()]
    assert len(canonical) == len(lifecycle) == 1
    assert canonical[0].exc_info is not None and canonical[0].exc_info[1] is cause
    assert all("full result" not in record.getMessage() for record in caplog.records)
