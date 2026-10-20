from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from codex_runtime_fixtures import declare_codex_tool_step
from core import turn_provider_call
from core.native_turn_control import TurnCancelEvent
from core.turn_core import TurnIdentity, persist_turn_start
from tools.base import Tool
from tools.registry import ToolRegistry


class _CaptureTool(Tool):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "capture_turn"

    @property
    def description(self) -> str:
        return "Capture backend-owned turn context."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "ok"


def _seed_active_turn(
    db_path,
    *,
    thread_id: str = "identity-thread",
    turn_id: str = "identity-turn",
) -> TurnIdentity:
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,model,project_id,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            thread_id,
            "identity-user",
            "test/model",
            "identity-project",
            "t",
            "t",
        ),
    )
    db.commit()
    admission = admit_input(
        thread_id,
        role="user",
        content="test",
        intent="apply_now",
        message_id=f"{turn_id}:input",
        turn_id=turn_id,
        boot_epoch="test-boot",
        admitted_at="t",
        db=db,
    )
    assert admission.batch is not None
    attempt_id = f"{turn_id}:write"
    begin_batch_write(
        admission.batch, attempt_id=attempt_id, started_at="t", db=db,
    )
    settle_batch_write(attempt_id, outcome="accepted", db=db)
    identity = persist_turn_start(
        turn_id,
        thread_id,
        "test/model",
        "test",
        db=db,
    )
    db.commit()
    db.close()
    return identity


@pytest.mark.asyncio
async def test_registry_injects_only_validated_private_turn_identity(isolated_db):
    from infra import state

    identity = _seed_active_turn(isolated_db)
    registry = ToolRegistry()
    tool = _CaptureTool()
    registry.register(tool)
    supplied = {
        "value": "safe",
        "_thread_turn_id": "caller-forged",
        "_host_access_turn_identity": "caller-forged",
    }

    thread_token = state._current_thread_id.set("identity-thread")
    turn_token = state._current_turn_identity.set(identity)
    try:
        result = await registry.execute("capture_turn", supplied)
    finally:
        state._current_turn_identity.reset(turn_token)
        state._current_thread_id.reset(thread_token)

    assert result == "ok"
    assert tool.calls == [{
        "value": "safe",
        "_thread_id": "identity-thread",
        "_thread_project_id": "identity-project",
        "_host_access_turn_identity": identity,
    }]
    assert tool.calls[0]["_host_access_turn_identity"] is identity
    assert supplied["_thread_turn_id"] == "caller-forged"
    assert supplied["_host_access_turn_identity"] == "caller-forged"


@pytest.mark.asyncio
async def test_registry_rejects_stale_identity_without_inferring_successor(
    isolated_db,
):
    from core.turn_core import persist_turn_terminal
    from infra import state
    from infra.thread_driver import admit_input

    stale = _seed_active_turn(isolated_db)
    db = sqlite3.connect(isolated_db)
    db.row_factory = sqlite3.Row
    persist_turn_terminal(stale.turn_id, "completed", db=db)
    admission = admit_input(
        "identity-thread",
        role="user",
        content="successor",
        intent="apply_now",
        message_id="successor-input",
        turn_id="successor-turn",
        boot_epoch="test-boot",
        admitted_at="t2",
        db=db,
    )
    assert admission.batch is not None
    successor = admission.batch.identity
    db.commit()
    db.close()
    registry = ToolRegistry()
    tool = _CaptureTool()
    registry.register(tool)

    thread_token = state._current_thread_id.set("identity-thread")
    turn_token = state._current_turn_identity.set(stale)
    try:
        result = await registry.execute("capture_turn", {"value": "safe"})
    finally:
        state._current_turn_identity.reset(turn_token)
        state._current_thread_id.reset(thread_token)

    assert successor != stale
    assert result.startswith(
        "Error: Tool execution turn identity is no longer active."
    )
    assert tool.calls == []


@pytest.mark.asyncio
async def test_harness_bridge_propagates_and_resets_exact_turn_identity(
    isolated_db,
):
    from infra import state
    from infra.database import get_db
    from subagents.child_thread import persist_assistant_step
    from subagents.harness_tool_bridge import execute_tool

    identity = _seed_active_turn(isolated_db)
    db = get_db()
    step_id = persist_assistant_step(
        "identity-thread",
        content="",
        tool_uses=[{
            "id": "call-identity", "name": "capture_turn",
            "arguments": {"value": "safe"},
        }],
        turn_id=identity.turn_id,
        db=db,
    )
    db.commit()
    db.close()
    registry = ToolRegistry()
    tool = _CaptureTool()
    registry.register(tool)

    result = await execute_tool(
        registry,
        thread_id="identity-thread",
        turn_identity=identity,
        invocation_id="call-identity-invocation",
        provider_tool_call_id="call-identity",
        assistant_step_id=step_id,
        name="capture_turn",
        arguments={"value": "safe"},
    )

    assert result.startswith("ok")
    assert result.delivery.content == "ok"
    assert tool.calls[0]["_host_access_turn_identity"] is identity
    assert state._current_thread_id.get() is None
    assert state._current_turn_identity.get() is None


@pytest.mark.asyncio
async def test_cc_bridge_passes_live_exact_turn_identity(monkeypatch):
    from api import cc_attach

    identity = TurnIdentity("cc-turn", 7)
    result = SimpleNamespace(turn_identity=identity, delivery=object())
    execute = AsyncMock(return_value=result)
    settle = AsyncMock(return_value=False)
    monkeypatch.setattr(cc_attach, "execute_tool", execute)
    monkeypatch.setattr(
        "subagents.harness_tool_bridge.settle_cc_tool_result",
        settle,
    )
    live = SimpleNamespace(
        tool_registry=object(),
        corsia_turn=identity,
        tool_step_ids={"cc-call": "cc-step"},
        admitted_tool_tasks=set(),
        tool_execution_admitted=False,
    )
    # Declared-call binding has its own negatives; this pins identity forwarding.
    monkeypatch.setattr(
        cc_attach, "_claim_declared_tool_call",
        AsyncMock(return_value=("cc-call", "cc-step")),
    )

    assert await cc_attach._execute_admitted_tool(
        live,
        "cc-thread",
        {
            "name": "capture_turn",
            "arguments": {"value": "safe"},
            "invocation_id": "cc-invocation",
            "provider_tool_call_id": "cc-call",
        },
    ) is result

    execute.assert_awaited_once_with(
        live.tool_registry,
        thread_id="cc-thread",
        turn_identity=identity,
        invocation_id="cc-invocation",
        provider_tool_call_id="cc-call",
        assistant_step_id="cc-step",
        name="capture_turn",
        arguments={"value": "safe"},
        harness="claude-code",
    )
    assert live.admitted_tool_tasks == set()
    settle.assert_awaited_once_with(live, "cc-thread", result, identity)


@pytest.mark.asyncio
async def test_cc_checkpoint_tool_skips_canonical_turn_lineage(monkeypatch):
    import asyncio
    from api import cc_attach
    from core import checkpoint_runtime as runtime

    execute = AsyncMock(return_value="checkpoint result")
    monkeypatch.setattr(cc_attach, "execute_tool", execute)
    live = SimpleNamespace(tool_registry=object())

    async def run(operation, call_id, name, arguments):
        assert (call_id, name, arguments) == (
            "checkpoint-call", "capture_turn", {"value": "safe"},
        )
        return await operation()

    task = asyncio.create_task(asyncio.Event().wait())
    monkeypatch.setitem(runtime._tasks, "cp-1", runtime.ForkHandle(
        task, asyncio.Event(), "cc-thread", run,
    ))
    try:
        result = await cc_attach._execute_admitted_tool(
            live, "cc-thread",
            {"name": "capture_turn", "arguments": {"value": "safe"},
             "provider_tool_call_id": "checkpoint-call"},
            "cp-1",
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert result == "checkpoint result"
    execute.assert_awaited_once_with(
        live.tool_registry,
        thread_id="cc-thread",
        checkpoint_id="cp-1",
        name="capture_turn",
        arguments={"value": "safe"},
        harness="claude-code",
    )


@pytest.mark.asyncio
async def test_codex_dynamic_tool_passes_runtime_bound_turn_identity(monkeypatch):
    from subagents import harness_tool_bridge
    from subagents.codex_app_server import CodexAppServerSession

    identity = TurnIdentity("codex-turn", 9)
    execute = AsyncMock(return_value="ok")
    monkeypatch.setattr(harness_tool_bridge, "execute_tool", execute)
    registry = object()
    session = CodexAppServerSession(
        binary="codex",
        task=None,
        tool_registry=registry,
        corsia_thread_id="codex-thread",
        corsia_turn_identity=identity,
    )
    step_id = declare_codex_tool_step(session, "call-1", persist=False)

    result, error = await session._handle_dynamic_tool_request({
        "tool": "capture_turn",
        "arguments": {"value": "safe"},
        "callId": "call-1",
    })

    assert error is None
    assert result == {
        "contentItems": [{"type": "inputText", "text": "ok"}],
        "success": True,
    }
    execute.assert_awaited_once_with(
        registry,
        thread_id="codex-thread",
        turn_identity=identity,
        invocation_id="call-1",
        provider_tool_call_id="call-1",
        assistant_step_id=step_id,
        name="capture_turn",
        arguments={"value": "safe"},
        harness="codex",
    )


@pytest.mark.asyncio
async def test_codex_start_binds_exact_identity_onto_reused_app_session(
    isolated_db,
):
    from infra.thread_driver import admit_input
    from subagents import codex_runtime

    _seed_active_turn(
        isolated_db,
        thread_id="unrelated-thread",
        turn_id="unrelated-turn",
    )
    db = sqlite3.connect(isolated_db)
    db.row_factory = sqlite3.Row
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,model,project_id,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            "codex-thread",
            "identity-user",
            "codex/gpt-5-codex",
            None,
            "t",
            "t",
        ),
    )
    db.commit()
    admission = admit_input(
        "codex-thread",
        role="user",
        content="hello",
        intent="apply_now",
        message_id="codex-input",
        turn_id="codex-turn",
        boot_epoch="test-boot",
        admitted_at="t",
        db=db,
    )
    assert admission.batch is not None
    db.commit()
    db.close()
    app_session = SimpleNamespace(corsia_turn_identity=None)
    live = codex_runtime._LiveCodex(
        codex_thread_id="provider-thread",
        session=app_session,
        parent_thread_id=None,
        name="codex",
        model="codex/gpt-5-codex",
    )

    identity = await codex_runtime._start_live_turn(
        "codex-thread",
        live,
        "hello",
        successor_turn_id="codex-turn",
        bcast=lambda *_args: None,
    )

    assert identity is not None
    assert live.corsia_turn is identity
    assert app_session.corsia_turn_identity is identity


@pytest.mark.asyncio
async def test_native_turn_sets_and_resets_exact_turn_context(monkeypatch):
    from core import turn_iteration, turn_iteration_context, turns, turns_steps
    from infra import state

    identity = TurnIdentity("native-turn", 11)
    cancel_event = TurnCancelEvent()
    cancel_event.bind_turn_identity(identity)
    observed: list[object] = []

    class _Tools:
        @staticmethod
        def get_definitions() -> list[dict]:
            return []

    class _Agent:
        model = "test/model"
        max_iterations = 1
        tools = _Tools()

        async def _connect_mcp(self) -> None:
            return None

        @staticmethod
        def _strip_think(content: str) -> str:
            return content

    async def call_llm(*_args, **_kwargs):
        observed.append(state._current_turn_identity.get())
        return (
            SimpleNamespace(
                content="done",
                finish_reason="stop",
                has_tool_calls=False,
                usage={"prompt_tokens": 1},
                ttft_ms=None,
            ),
            False,
        )

    monkeypatch.setattr(
        turns_steps,
        "lookup_thread_project_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        turn_iteration,
        "prepare_turn_context",
        AsyncMock(return_value=([{"role": "user", "content": "hello"}], {})),
    )
    monkeypatch.setattr(turn_provider_call, "call_llm", call_llm)
    monkeypatch.setattr(turn_iteration, "run_in_db", AsyncMock(return_value=None))

    await turns.run_turn_with_steps(
        _Agent(),
        "hello",
        "native-thread",
        "web",
        "native-thread",
        AsyncMock(),
        cancel_event,
        model="test/model",
        turn_provider={},
        prepared_context=SimpleNamespace(
            messages=[{"role": "user", "content": "hello"}],
            context_usage={},
            tool_definitions=[],
            context_identity=None,
            fit_ledger=None,
        ),
    )

    assert observed == [identity]
    assert observed[0] is identity
    assert state._current_turn_identity.get() is None
