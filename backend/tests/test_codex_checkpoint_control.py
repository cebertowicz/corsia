"""Checkpoint tool replies use stock live usage without blocking their pipe reader."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from codex_runtime_fixtures import declare_codex_tool_step
from core.checkpoint_codex_context import CodexCheckpointContext
from subagents.codex_app_server import CodexAppServerSession
from test_checkpoint_codex_capacity import target


@pytest.mark.asyncio
async def test_checkpoint_capture_propagates_completed_tool_failure():
    from core.checkpoint_codex_result import CodexCheckpointCapture
    from tools.recorded_call import CompletedToolCall, CompletedToolFailure

    capture = CodexCheckpointCapture()
    failure = CompletedToolFailure((CompletedToolCall(
        name="edit", result="private completed result", checkpoint_id="checkpoint",
    ),))

    await capture.on_exit(-9, failure)

    with pytest.raises(CompletedToolFailure) as raised:
        await capture.terminal
    assert raised.value is failure


@pytest.mark.asyncio
@pytest.mark.parametrize("output, expected_calls", [(10, 1), (100, 0)])
@pytest.mark.parametrize("ordinary", [False, True])
async def test_stock_receipt_precedes_tool_execution_and_replayed_rpc_runs_once(
    monkeypatch, tmp_path, output, expected_calls, ordinary,
):
    writes, executed, raw, observed_usage = asyncio.Queue(), [], [], []
    context = CodexCheckpointContext(target(100), 1)

    class Pipe:
        def write(self, payload):
            writes.put_nowait(json.loads(payload))

        async def drain(self):
            pass

    async def execute(*_args, **_kwargs):
        executed.append(True)
        if ordinary:
            observed_usage.append(session.peek_active_usage())
        return "done"

    async def observe(frame):
        raw.append(frame)

    def boundary(reply, usage=None):
        if usage is not None:
            context.record_receipt(usage)
        return context.fits if reply is None else context.accept_tool_result(reply)

    monkeypatch.setattr("subagents.harness_tool_bridge.execute_tool", execute)
    session = CodexAppServerSession(
        binary="unused", task=None, codex_thread_id="provider",
        checkpoint_id=None if ordinary else "checkpoint",
        corsia_thread_id="thread", tool_registry=object(),
        checkpoint_tool_result_fits=None if ordinary else boundary, on_raw_message=observe,
    )
    declare_codex_tool_step(session, "edit", persist=False)
    session._active_turn_id = "current-turn"
    session._proc = SimpleNamespace(stdin=Pipe(), returncode=None)
    session._proc.kill = lambda: setattr(session._proc, "returncode", -9)
    from test_codex_durable_receipts import group, lines
    path = tmp_path / "rollout.jsonl"
    path.touch()
    session._tool_requests.rollout_path = str(path)
    await session._tool_requests.prepare_turn()
    call = {"id": 900, "method": "item/tool/call", "params": {
        "threadId": "provider", "turnId": "current-turn", "callId": "edit",
        "tool": "edit", "arguments": {},
    }}
    dispatched = asyncio.create_task(session._handle_message(call))
    try:
        await dispatched
        await asyncio.sleep(0)
        assert executed == []
        assert writes.empty()
        records = group(turn="current-turn", calls=("edit",))
        records[-1]["payload"]["usage"].update(input_tokens=50, output_tokens=output)
        path.write_bytes(lines(*records))
        await session._handle_message(call)  # Replayed exact pending RPC runs once.
        await asyncio.wait_for(dispatched, 1)
        await asyncio.wait_for(session._dynamic_tool_requests_idle.wait(), 1)
        expected_calls = 1 if ordinary else expected_calls
        assert len(executed) == expected_calls
        if ordinary:
            assert observed_usage[0]["input_tokens"] == 50
            assert set(session._usage_by_turn) == {"current-turn"}
        assert session._turn_volume.spent_readings("current-turn") == 1
        if expected_calls:
            reply = writes.get_nowait()
            assert reply["id"] == 900 and reply["result"]["success"]
        else:
            assert session._closing and session._proc.returncode == -9
        assert writes.empty()  # No duplicate query or duplicate reply.
    finally:
        dispatched.cancel()
        await asyncio.gather(dispatched, return_exceptions=True)
        if getattr(session, "_tool_requests", None) is not None:
            await session._tool_requests.close()


def test_same_receipt_keeps_aggregate_tools_but_new_response_resets_it():
    context = CodexCheckpointContext(target(1000), 0)
    first = {"last": {"inputTokens": 100, "outputTokens": 10},
             "total": {"inputTokens": 19000100, "outputTokens": 1000}}
    context.record_receipt(first)
    context.accept_tool_result({"contentItems": [{"type": "inputText", "text": " x" * 100}]})
    after_tool = context.tokens
    context.record_receipt(first)
    assert context.tokens == after_tool and after_tool > 210
    context.record_receipt({"last": {"inputTokens": 230, "outputTokens": 10},
                            "total": {"inputTokens": 19000330, "outputTokens": 1010}})
    assert context.tokens == 240


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True])
async def test_missing_usage_or_stop_closes_control_wait_without_a_tool_reply(tmp_path, stop):
    writes, events = [], []

    class Pipe:
        def write(self, payload):
            writes.append(json.loads(payload))

        async def drain(self):
            pass

    async def on_event(frame):
        events.append(frame)

    session = CodexAppServerSession(
        binary="unused", task=None, codex_thread_id="provider", checkpoint_id="checkpoint",
        checkpoint_tool_result_fits=lambda *_args: True, on_event=on_event,
    )
    session._active_turn_id = "turn"
    session._proc = SimpleNamespace(stdin=Pipe(), returncode=None)
    session._proc.kill = lambda: setattr(session._proc, "returncode", -9)
    if stop:
        path = tmp_path / "rollout.jsonl"
        path.touch()
        session._tool_requests.rollout_path = str(path)
        await session._tool_requests.prepare_turn()
    await session._handle_message({"id": 900, "method": "item/tool/call", "params": {
        "threadId": "provider", "turnId": "turn", "callId": "edit", "tool": "edit",
    }})
    await asyncio.sleep(0)  # Start the owned callback; there is deliberately no control reply.
    if stop:
        session._request_terminate()
        await session._tool_requests.close()
    await asyncio.wait_for(session._dynamic_tool_requests_idle.wait(), 1)
    assert session._closing is stop and session._proc.returncode == -9
    assert writes == []
    assert not session._pending and not session._tool_requests.tasks
    assert not events
    if not stop:
        assert b"Tool usage/control failed" in session._stderr


@pytest.mark.asyncio
async def test_tool_initiated_teardown_can_finish_its_same_turn_cutover(monkeypatch, tmp_path):
    """Draining the old reader must not cancel the callback that is replacing it."""
    stdout = asyncio.StreamReader()
    finished, resumed, writes = asyncio.Event(), [], []

    class Process:
        returncode = None

        def kill(self):
            self.returncode = -9
            stdout.feed_eof()

        async def wait(self):
            return self.returncode

    class Pipe:
        def write(self, data):
            query = json.loads(data)
            writes.append(query)

        async def drain(self):
            pass

    session = CodexAppServerSession(binary="unused", task=None,
        codex_thread_id="provider", corsia_thread_id="thread", tool_registry=object())
    declare_codex_tool_step(session, "edit", persist=False)
    session._proc = Process()
    session._proc.stdin, session._proc.stdout = Pipe(), stdout
    session._active_turn_id = "current"
    from test_codex_durable_receipts import group, lines
    path = tmp_path / "rollout.jsonl"
    path.touch()
    session._tool_requests.rollout_path = str(path)
    await session._tool_requests.prepare_turn()
    path.write_bytes(lines(*group(turn="current", calls=("edit",))))

    async def execute(*_args, **_kwargs):
        try:
            await session.terminate(timeout=0.2)
            resumed.append("same-turn continuation")
            return "retained result"
        finally:
            finished.set()

    monkeypatch.setattr("subagents.harness_tool_bridge.execute_tool", execute)
    session._reader = asyncio.create_task(session._read_loop())
    stdout.feed_data(json.dumps({"id": 900, "method": "item/tool/call", "params": {
        "threadId": "provider", "turnId": "current", "callId": "edit", "tool": "edit",
    }}).encode() + b"\n")
    try:
        await asyncio.wait_for(finished.wait(), 1)
        assert resumed == ["same-turn continuation"]
        assert session._proc.returncode == -9
        assert writes == []
    finally:
        await session.terminate(timeout=0.2)
