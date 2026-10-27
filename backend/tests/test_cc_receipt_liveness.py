"""Receipt waiting follows provider completion and transport, never elapsed time."""

import asyncio

import pytest

from test_checkpoint_cc_capacity import _context


def _partial(context):
    context.observe({"type": "stream_event", "event": {
        "type": "message_start", "message": {"id": "response", "usage": {
            "input_tokens": 100, "output_tokens": 1,
        }},
    }})
    context.observe({"type": "stream_event", "event": {
        "type": "content_block_start", "content_block": {
            "type": "tool_use", "id": "call", "name": "read_file", "input": {},
        },
    }})


def _session():
    from subagents.cc_stream import CCStreamSession
    from test_checkpoint_external_adapters import WaitingProcess

    session = CCStreamSession(binary="unused", session_id="receipt")
    session._proc = WaitingProcess()
    session._proc.kill = lambda: session._proc._finish(-9)
    session._proc.stdout = asyncio.StreamReader()
    session.tool_context = _context()
    _partial(session.tool_context)
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint", [False, True])
@pytest.mark.parametrize("completion", ["message_stop", "result"])
async def test_completed_response_without_final_usage_fails_immediately(checkpoint, completion):
    from core.fork_cc_io import _ToolBoundary
    from core.fork_types import ProviderForkRejected
    from test_checkpoint_external_adapters import WaitingProcess

    session = _session()
    session.tool_context.observe({"type": "stream_event", "event": {"type": "message_stop"}}
                                 if completion == "message_stop" else {"type": "result", "is_error": True})
    process = WaitingProcess()
    boundary = _ToolBoundary(process, session.tool_context, asyncio.Event())
    operation = (boundary.execute(lambda: asyncio.sleep(0, result="result"), "call", "read_file", {})
                 if checkpoint else session.record_tool_reply("call", "result", session.tool_context.target))
    with pytest.raises((RuntimeError, ProviderForkRejected), match="complete.*context"):
        await asyncio.wait_for(operation, 0.2)
    if checkpoint:
        assert process.returncode is not None


@pytest.mark.asyncio
async def test_checkpoint_receipt_ready_before_usage_persistence_waits_for_tool():
    from core.fork_cc_io import _ToolBoundary
    from test_checkpoint_external_adapters import WaitingProcess

    context = _context()
    _partial(context)
    context.observe({"type": "stream_event", "event": {
        "type": "message_delta", "usage": {"output_tokens": 10},
    }})
    replied = asyncio.Event()

    async def usage(*_args):
        await replied.wait()

    async def execute():
        replied.set()
        return "result"

    boundary = _ToolBoundary(WaitingProcess(), context, asyncio.Event(), on_usage=usage)
    tool = asyncio.create_task(boundary.execute(execute, "call", "read_file", {}))
    try:
        await asyncio.wait_for(boundary.observe({"type": "stream_event", "event": {
            "type": "message_stop",
        }}), 0.2)
        assert await asyncio.wait_for(tool, 0.2) == "result"
    finally:
        tool.cancel()
        await asyncio.gather(tool, return_exceptions=True)


@pytest.mark.asyncio
async def test_stdout_eof_releases_ordinary_receipt_waiter():
    session = _session()
    session._proc.stdout.feed_eof()
    with pytest.raises(RuntimeError, match="ended"):
        await asyncio.wait_for(session.record_tool_reply("call", "result", session.tool_context.target), 0.2)


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint", [False, True])
async def test_early_rpc_can_wait_after_previous_response_finished(checkpoint):
    from core.fork_cc_io import _ToolBoundary

    session = _session()
    session.tool_context.tool_ids = {"previous-call"}
    session.tool_context.finished = True
    boundary = _ToolBoundary(session._proc, session.tool_context, asyncio.Event())
    operation = (boundary.execute(lambda: asyncio.sleep(0, result="result"), "call", "read_file", {})
                 if checkpoint
                 else session.record_tool_reply("call", "result", session.tool_context.target))
    pending = asyncio.create_task(operation)
    try:
        await asyncio.sleep(0)
        assert not pending.done()
        record = {"type": "assistant", "message": {
            "id": "next-response", "stop_reason": "tool_use",
            "usage": {"input_tokens": 100, "output_tokens": 10},
            "content": [{"type": "tool_use", "id": "call", "name": "read_file", "input": {}}],
        }}
        await (boundary.observe(record) if checkpoint else session._dispatch_raw_event(record))
        await asyncio.wait_for(pending, 0.2)
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_checkpoint_stdout_eof_releases_pending_tool_before_process_exit():
    import sys

    from core.fork_cc_io import _ToolBoundary, _read_stdout
    from core.fork_types import ProviderForkRejected

    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import os,signal; os.close(1); signal.pause()",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    boundary = _ToolBoundary(process, _context(), asyncio.Event())
    _partial(boundary.context)
    tool = asyncio.create_task(
        boundary.execute(lambda: asyncio.sleep(0, result="unsafe"), "call", "read_file", {})
    )
    await asyncio.sleep(0)
    await _read_stdout(process, boundary, bytearray())
    try:
        with pytest.raises(ProviderForkRejected, match="ended"):
            await asyncio.wait_for(tool, 0.2)
        assert process.returncode is not None
    finally:
        tool.cancel()
        await asyncio.gather(tool, return_exceptions=True)
        if process.returncode is None:
            process.kill()
        await process.wait()


@pytest.mark.asyncio
async def test_checkpoint_result_fences_unknown_receipt_before_process_exit():
    from core.fork_cc_io import _ToolBoundary
    from core.fork_types import ProviderForkRejected
    from test_checkpoint_external_adapters import WaitingProcess

    process = WaitingProcess()
    boundary = _ToolBoundary(process, _context(), asyncio.Event())
    tool = asyncio.create_task(
        boundary.execute(lambda: asyncio.sleep(0, result="unsafe"), "unknown-call", "read_file", {})
    )
    try:
        await asyncio.sleep(0)
        await boundary.observe({"type": "result", "is_error": True})
        with pytest.raises(ProviderForkRejected, match="ended"):
            await asyncio.wait_for(tool, 0.2)
        assert process.returncode is not None
    finally:
        tool.cancel()
        await asyncio.gather(tool, return_exceptions=True)


@pytest.mark.asyncio
async def test_streaming_response_survives_old_deadline_for_both_boundaries(monkeypatch):
    """A delayed valid tail remains admissible after the former 30-second cutoff."""
    from core.fork_cc_io import _ToolBoundary
    from test_checkpoint_external_adapters import WaitingProcess

    session = _session()
    process = WaitingProcess()
    boundary = _ToolBoundary(process, _context(), asyncio.Event())
    _partial(boundary.context)
    ordinary = asyncio.create_task(session.record_tool_reply("call", "result", session.tool_context.target))
    checkpoint = asyncio.create_task(
        boundary.execute(lambda: asyncio.sleep(0, result="result"), "call", "read_file", {})
    )
    try:
        loop = asyncio.get_running_loop()
        clock = loop.time
        await asyncio.sleep(0)  # Start both waiters before advancing their clock.
        monkeypatch.setattr(loop, "time", lambda: clock() + 30.1)
        await asyncio.sleep(0.001)  # Let any expired deadline callbacks run.
        assert not ordinary.done(), "active foreground response was timed out"
        assert not checkpoint.done(), "active checkpoint response was timed out"
        for owner in (session.tool_context, boundary.context):
            owner.observe({"type": "stream_event", "event": {
                "type": "message_delta", "usage": {"output_tokens": 10},
            }})
            owner.observe({"type": "stream_event", "event": {"type": "message_stop"}})
        session._context_changed.set()
        boundary.changed.set()
        await asyncio.wait_for(ordinary, 1)
        assert await asyncio.wait_for(checkpoint, 1) == "result"
        assert process.returncode is None
    finally:
        for task in (ordinary, checkpoint):
            task.cancel()
        await asyncio.gather(ordinary, checkpoint, return_exceptions=True)
