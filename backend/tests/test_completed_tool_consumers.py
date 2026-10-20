"""Completed mutations survive Native and Codex publication boundaries."""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from tools.recorded_call import CompletedToolFailure
from core.checkpoint_fork_kind import CHECKPOINT_KIND


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["persistence", "projection", "step_done"])
async def test_native_batch_retains_every_completed_effect(isolated_db, tmp_path, monkeypatch, fault):
    from core.agent import CORSIAAgent
    from core.step_handler import StepHandler
    from core.turns_steps import execute_tool_calls
    from infra import state
    from infra.database import get_db
    from tests.plan_execution_helpers import ProbeTool, registry_of, selected_plan
    from tests.test_cc_tool_result_echo_binding import _seed_thread

    db = get_db()
    identity = _seed_thread(db)
    selected_plan(db, "thread-1")
    first, second = ProbeTool("first", result="first result"), ProbeTool("second", result="second result")
    agent = CORSIAAgent(workspace=tmp_path)
    agent.tools = registry_of(first, second)
    response = SimpleNamespace(content="", reasoning_content=None, tool_calls=[
        SimpleNamespace(id="call-first", name="first", arguments={}),
        SimpleNamespace(id="call-second", name="second", arguments={}),
    ])
    frames = []

    async def broadcast(frame):
        frames.append(frame)

    handler = StepHandler("thread-1", {"model": "test/model"}, broadcast, identity.turn_id)
    handler.bind_turn_identity(identity)

    def failed(*args, **kwargs):
        raise RuntimeError("synthetic publication fault")

    async def on_step(kind, payload):
        if fault == "persistence" and kind == "tool_complete" and payload["id"] == "call-second":
            failed()
        if fault == "step_done" and kind == "tool_calls_done":
            failed()
        return await handler.handle(kind, payload)

    if fault == "projection":
        monkeypatch.setattr("core.tool_result_projection.render_tool_result_for_model", failed)
    thread_token = state._current_thread_id.set("thread-1")
    turn_token = state._current_turn_identity.set(identity)
    try:
        with pytest.raises(CompletedToolFailure) as raised:
            await execute_tool_calls(agent, response, [], "thread-1", asyncio.Event(), on_step, [],
                                     "test/model", tool_result_budget_tokens=1000)
    finally:
        state._current_turn_identity.reset(turn_token)
        state._current_thread_id.reset(thread_token)
    assert first.calls == second.calls == 1
    completed = raised.value.completed
    assert [(c.name, c.result, c.provider_tool_call_id) for c in completed] == [
        ("first", "first result", "call-first"), ("second", "second result", "call-second"),
    ]
    assert all(c.thread_id == "thread-1" and c.turn_id == identity.turn_id for c in completed)
    rows = db.execute("SELECT content FROM messages WHERE role='tool' ORDER BY seq").fetchall()
    expected = ["first result"] if fault == "persistence" else ["first result", "second result"]
    assert [r["content"] for r in rows] == expected
    assert [c.canonical.content for c in completed if c.canonical] == expected
    assert all(c.assistant_step_id for c in completed if c.canonical)
    if fault == "projection":
        from core.turn_core import persist_turn_terminal
        from infra.thread_driver import TerminalResult

        assert persist_turn_terminal(
            identity.turn_id, "error", raised.value, db=db,
        ) is TerminalResult.APPLIED
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [sqlite3.OperationalError, RuntimeError])
async def test_native_batch_survives_a_failed_dispatch_settlement(
    isolated_db, tmp_path, monkeypatch, error_type,
):
    """Both completed results are delivered; the unsettled row closes at Turn terminal."""
    from core.agent import CORSIAAgent
    from core.step_handler import StepHandler
    from core.turns_steps import execute_tool_calls
    from infra import state
    from infra.database import get_db
    from plans import service, tool_dispatches
    from tests.plan_execution_helpers import ProbeTool, dispatch_rows, registry_of, selected_plan
    from tests.test_cc_tool_result_echo_binding import _seed_thread

    db = get_db()
    identity = _seed_thread(db)
    selected_plan(db, "thread-1")
    first, second = ProbeTool("first", result="first result"), ProbeTool("second", result="second result")
    agent = CORSIAAgent(workspace=tmp_path)
    agent.tools = registry_of(first, second)
    response = SimpleNamespace(content="", reasoning_content=None, tool_calls=[
        SimpleNamespace(id="call-first", name="first", arguments={}),
        SimpleNamespace(id="call-second", name="second", arguments={}),
    ])

    async def broadcast(frame):
        return None

    handler = StepHandler("thread-1", {"model": "test/model"}, broadcast, identity.turn_id)
    handler.bind_turn_identity(identity)
    original = tool_dispatches.settle

    def settle(*args, **kwargs):
        if second.calls:
            raise error_type("dispatch settlement unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr("plans.public.settle_tool_dispatch", settle)
    thread_token = state._current_thread_id.set("thread-1")
    turn_token = state._current_turn_identity.set(identity)
    try:
        await execute_tool_calls(agent, response, [], "thread-1", asyncio.Event(), handler.handle, [],
                                 "test/model", tool_result_budget_tokens=1000)
    finally:
        state._current_turn_identity.reset(turn_token)
        state._current_thread_id.reset(thread_token)
    assert first.calls == second.calls == 1
    rows = db.execute("SELECT content FROM messages WHERE role='tool' ORDER BY seq").fetchall()
    assert [r["content"] for r in rows] == ["first result", "second result"]
    [settled, still_open] = dispatch_rows(db)
    assert settled["settled_at"] is not None and still_open["settled_at"] is None
    with service._write(db):
        tool_dispatches.settle_turn(identity.turn_id, db=db)
    assert dispatch_rows(db)[1]["outcome"] == "unknown"
    db.close()


@pytest.mark.asyncio
async def test_native_checkpoint_stops_after_completed_settlement_failure(isolated_db, monkeypatch):
    from dataclasses import replace
    from core import checkpoint_native_fork as native
    from infra.database import get_db
    from llm.router import LLMResponse, ToolCallRequest
    from tests.plan_execution_helpers import ProbeTool, registry_of
    from tests.test_cc_tool_result_echo_binding import _seed_thread
    from tests.test_checkpoint_provider_adapters import _request, _source
    from tools.recorded_call import CompletedToolCall

    db = get_db()
    _seed_thread(db)
    tool = ProbeTool(result="private completed mutation")
    registry = registry_of(tool)
    provider_calls, usage = [], []

    async def completion(**kwargs):
        provider_calls.append(kwargs)
        return LLMResponse(content="", tool_calls=[ToolCallRequest(id="private-call", name="continuity_edit", arguments={})],
                           usage={"total_tokens": 150})

    async def agent():
        return SimpleNamespace(max_iterations=2, tools=registry)

    # Checkpoint tools usually have no foreground plan selection; inject the
    # completed infrastructure fault at the registry boundary they share.
    async def execute(name, args):
        result = await tool.execute()
        raise CompletedToolFailure((CompletedToolCall(name=name, result=result, thread_id="thread-1"),))

    async def paid(key, model, measured):
        usage.append(measured)

    monkeypatch.setattr(registry, "execute", execute)
    monkeypatch.setattr(native, "_get_agent", agent)
    monkeypatch.setattr(native, "_chat_completion", completion)
    monkeypatch.setattr(native, "_provider_params", lambda model: {"model": model})
    with pytest.raises(CompletedToolFailure) as raised:
        await native.invoke_checkpoint(replace(_request(_source(native)), on_usage=paid), asyncio.Event(), kind=CHECKPOINT_KIND)
    [completed] = raised.value.completed
    assert completed.provider_tool_call_id == "private-call"
    assert completed.checkpoint_id == "checkpoint-1"
    assert completed.result == "private completed mutation" and completed.canonical is None
    assert tool.calls == len(provider_calls) == 1
    assert usage == [{"total_tokens": 150}]
    assert db.execute("SELECT COUNT(*) FROM messages WHERE role='tool'").fetchone()[0] == 0
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint", [False, True])
async def test_codex_reply_failure_keeps_completed_output(isolated_db, monkeypatch, checkpoint):
    from core._tool_result_delivery_support import ToolResultBindingError
    from infra.database import get_db
    from subagents import codex_dynamic_tools
    from subagents.child_thread import persist_assistant_step
    from tests.plan_execution_helpers import ProbeTool, registry_of
    from tests.test_cc_tool_result_echo_binding import _seed_thread

    db = get_db()
    identity = _seed_thread(db)
    tool = ProbeTool(result="completed Codex mutation")
    step = persist_assistant_step("thread-1", content="", tool_uses=[{
        "id": "call", "name": tool.name, "arguments": {},
    }], turn_id=identity.turn_id, db=db)
    db.commit()
    session = SimpleNamespace(
        checkpoint_id="checkpoint" if checkpoint else None, checkpoint_tool_result_fits=None,
        tool_registry=registry_of(tool), corsia_thread_id="thread-1", corsia_turn_identity=identity,
        declared_tool_step_ids={"call": step}, catalog_model_id=None, model=None,
    )

    def failed(content):
        raise ToolResultBindingError("reply encoding unavailable")

    monkeypatch.setattr(codex_dynamic_tools, "dynamic_tool_response", failed)
    with pytest.raises(CompletedToolFailure) as raised:
        await codex_dynamic_tools.execute_dynamic_tool(session, tool.name, {}, {"callId": "call"})
    [completed] = raised.value.completed
    assert completed.result == "completed Codex mutation"
    assert completed.provider_tool_call_id == "call" and completed.invocation_id == "call"
    assert completed.checkpoint_id == session.checkpoint_id
    assert completed.canonical is None if checkpoint else completed.canonical.assistant_step_id == step
    assert tool.calls == 1
    assert not isinstance(raised.value, ToolResultBindingError)
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("fault", ["identity", "projection", "reply_settlement"])
async def test_codex_completed_failure_reaches_exact_terminal_owner(
    isolated_db, monkeypatch, managed, fault,
):
    from core._tool_result_delivery_support import ToolResultBindingError
    from infra.database import get_db
    from subagents import codex_runtime, harness_tool_bridge
    from subagents.codex_app_server import CodexAppServerSession
    from subagents.codex_runtime_state import _LIVE, _LiveCodex
    from subagents.codex_tool_requests import ToolRequests
    from subagents.child_thread import persist_assistant_step
    from tests.plan_execution_helpers import ProbeTool, registry_of
    from tests.test_cc_tool_result_echo_binding import _seed_thread
    from tests.test_codex_tool_control_lifecycle import Process, tool_call

    db = get_db()
    identity = _seed_thread(db)
    tool = ProbeTool("edit", result="completed Codex effect")
    step = persist_assistant_step("thread-1", content="", tool_uses=[{
        "id": "call-turn", "name": "edit", "arguments": {},
    }], turn_id=identity.turn_id, db=db)
    db.commit()
    frames = []
    proc = Process()
    session = CodexAppServerSession(
        binary="unused", task=None, codex_thread_id="provider", corsia_thread_id="thread-1",
        corsia_turn_identity=identity, tool_registry=registry_of(tool),
    )
    session._proc = proc
    session._track_active_turn("turn")
    if not managed:
        session._tool_requests = None
    live = _LiveCodex(session=session, parent_thread_id=None, name="test", codex_thread_id="provider",
                      corsia_turn=identity, catalog_model_id="codex/gpt-5.4")
    live.tool_step_ids["call-turn"] = step
    live.bind_app_server_session(session)
    live.tool_names["call-turn"] = "edit"
    live.tools_settled.clear()
    _LIVE.put("thread-1", live)

    async def usage(*args):
        return {"last": {"inputTokens": 100, "outputTokens": 10},
                "total": {"inputTokens": 100, "outputTokens": 10}}

    async def failed(*args, **kwargs):
        raise ToolResultBindingError("api_key=synthetic-private-binding")

    async def exited(code, stderr):
        await codex_runtime._finish_transport_exit("thread-1", live, session, code, stderr,
                                                  lambda _thread, frame: frames.append(frame))

    session._on_exit = exited
    monkeypatch.setattr(ToolRequests, "_usage", usage)
    if fault == "projection":
        monkeypatch.setattr(harness_tool_bridge, "_media_delivery", failed)
    if fault == "identity":
        monkeypatch.setattr(harness_tool_bridge, "_persist_delivery_off_loop", failed)
    if fault == "reply_settlement":
        monkeypatch.setattr(harness_tool_bridge, "settle_codex_tool_result", failed)
    session._reader = asyncio.create_task(session._read_loop())
    proc.send(tool_call())
    try:
        await asyncio.wait_for(asyncio.shield(session._reader), 2)
        assert proc.returncode is not None and not proc.writes
        assert tool.calls == 1
        turn = db.execute("SELECT status,error FROM active_turns WHERE id=?", (identity.turn_id,)).fetchone()
        assert turn["status"] == "error"
        assert "Tool execution completed" in turn["error"]
        assert "synthetic-private-binding" not in turn["error"]
        rows = db.execute("SELECT content FROM messages WHERE role='tool'").fetchall()
        assert [r[0] for r in rows] == ["completed Codex effect"]
        assert len([f for f in frames if f["type"] == "turn_complete"]) == 1
        assert _LIVE.get("thread-1") is None
    finally:
        await session.terminate(timeout=0.2)
        _LIVE.pop("thread-1", None)
        db.close()


@pytest.mark.asyncio
async def test_managed_codex_typed_failure_refuses_queued_second_effect(
    isolated_db, monkeypatch,
):
    """The first typed terminal fault closes queued managed-tool admission."""
    from core._tool_result_delivery_support import ToolResultBindingError
    from infra.database import get_db
    from subagents import codex_runtime, harness_tool_bridge
    from subagents.codex_app_server import CodexAppServerSession
    from subagents.codex_runtime_state import _LIVE, _LiveCodex
    from subagents.codex_tool_requests import ToolRequests
    from subagents.child_thread import persist_assistant_step
    from tests.plan_execution_helpers import ProbeTool, registry_of
    from tests.test_cc_tool_result_echo_binding import _seed_thread
    from tests.test_codex_tool_control_lifecycle import Process, tool_call

    class HeldEofProcess(Process):
        def kill(self):
            self.returncode = -9

    db = get_db()
    identity = _seed_thread(db)
    tool = ProbeTool("edit", result="first exact Codex result")
    step = persist_assistant_step("thread-1", content="", tool_uses=[{
        "id": "call-turn", "name": "edit", "arguments": {},
    }], turn_id=identity.turn_id, db=db)
    db.commit()
    proc = HeldEofProcess()
    frames = []
    session = CodexAppServerSession(
        binary="unused", task=None, codex_thread_id="provider",
        corsia_thread_id="thread-1", corsia_turn_identity=identity,
        tool_registry=registry_of(tool),
    )
    session._proc = proc
    session._track_active_turn("turn")
    live = _LiveCodex(
        session=session, parent_thread_id=None, name="test", codex_thread_id="provider",
        corsia_turn=identity, catalog_model_id="codex/gpt-5.4",
    )
    live.tool_step_ids["call-turn"] = step
    live.bind_app_server_session(session)
    live.tool_names["call-turn"] = "edit"
    live.tools_settled.clear()
    _LIVE.put("thread-1", live)

    async def usage(*_args):
        return {"last": {"inputTokens": 100, "outputTokens": 10},
                "total": {"inputTokens": 100, "outputTokens": 10}}

    async def failed(**_kwargs):
        raise ToolResultBindingError("first canonical persistence failed")

    async def exited(code, stderr):
        await codex_runtime._finish_transport_exit(
            "thread-1", live, session, code, stderr,
            lambda _thread, frame: frames.append(frame),
        )

    monkeypatch.setattr(ToolRequests, "_usage", usage)
    monkeypatch.setattr(harness_tool_bridge, "_persist_delivery_off_loop", failed)
    session._on_exit = exited
    await session._tool_requests.lock.acquire()
    session._reader = asyncio.create_task(session._read_loop())
    first = tool_call(rpc=900)
    second = tool_call(rpc=901)
    second["params"]["callId"] = "call-second"
    proc.send(first)
    proc.send(second)
    try:
        for _ in range(100):
            if len(session._tool_requests.tasks) == 2:
                break
            await asyncio.sleep(0)
        assert len(session._tool_requests.tasks) == 2
        session._tool_requests.lock.release()
        await asyncio.wait_for(session._dynamic_tool_requests_idle.wait(), 1)
        assert tool.calls == 1
        assert not session._dynamic_tool_admission_open
        proc.stdout.feed_eof()
        await asyncio.wait_for(asyncio.shield(session._reader), 2)
        assert [row[0] for row in db.execute(
            "SELECT content FROM messages WHERE role='tool'"
        )] == ["first exact Codex result"]
        assert len([frame for frame in frames if frame["type"] == "turn_complete"]) == 1
    finally:
        if session._tool_requests.lock.locked():
            session._tool_requests.lock.release()
        await session.terminate(timeout=0.2)
        _LIVE.pop("thread-1", None)
        db.close()
