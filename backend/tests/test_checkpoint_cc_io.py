"""A real disposable pipe must die before an oversized Claude tool reply returns."""

import asyncio
import json
import sys
from dataclasses import replace

import pytest
from core.checkpoint_fork_kind import CHECKPOINT_KIND

from native_checkpoint_fixtures import seed_vendor_checkpoint_thread
from test_checkpoint_external_adapters import _request, _target

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login
DISCOVERED_MODELS = {'claude-code': ('catalog-claude-code',), 'codex': ('catalog-codex',)}


@pytest.mark.asyncio
async def test_checkpoint_tool_infrastructure_failure_stops_its_writer_before_error():
    from core.fork_cc_io import _ToolBoundary
    from test_checkpoint_cc_capacity import _context
    from test_checkpoint_external_adapters import WaitingProcess

    process = WaitingProcess()
    context = _context()
    context.observe({"type": "assistant", "message": {
        "id": "response", "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "edit", "name": "edit", "input": {}}],
        "usage": {"input_tokens": 100, "output_tokens": 10},
    }})
    boundary = _ToolBoundary(process, context, asyncio.Event())

    async def unavailable():
        raise RuntimeError("checkpoint tool storage unavailable")

    with pytest.raises(RuntimeError, match="checkpoint tool storage unavailable"):
        await boundary.execute(unavailable, "edit", "edit", {})
    assert process.returncode is not None, "checkpoint MCP error escaped a live writer"
    assert boundary.closed and boundary.failure is not None


@pytest.mark.asyncio
async def test_checkpoint_tool_rejects_substitution_and_replay_before_execution():
    from core._tool_result_delivery_support import ToolResultBindingError
    from core.fork_cc_io import _ToolBoundary
    from test_checkpoint_cc_capacity import _context
    from test_checkpoint_external_adapters import WaitingProcess

    context = _context()
    context.observe({"type": "assistant", "message": {
        "id": "response", "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "call-1", "name": "mcp__corsia__read_file",
                     "input": {"path": "allowed.md"}}],
        "usage": {"input_tokens": 100, "output_tokens": 10},
    }})
    boundary = _ToolBoundary(WaitingProcess(), context, asyncio.Event())
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        return "ok"

    with pytest.raises(ToolResultBindingError):
        await boundary.execute(operation, "call-1", "delegate_send", {"path": "allowed.md"})
    with pytest.raises(ToolResultBindingError):
        await boundary.execute(operation, "call-1", "read_file", {"path": "forged.md"})
    assert calls == 0
    assert await boundary.execute(operation, "call-1", "read_file", {"path": "allowed.md"}) == "ok"
    with pytest.raises(ToolResultBindingError):
        await boundary.execute(operation, "call-1", "read_file", {"path": "allowed.md"})
    assert calls == 1


@pytest.mark.asyncio
async def test_failed_executor_registration_closes_the_spawned_checkpoint():
    from core.fork_cc_io import communicate_fork
    from test_checkpoint_external_adapters import WaitingProcess

    process = WaitingProcess()

    async def rejected(_executor):
        raise RuntimeError("checkpoint no longer owned")

    with pytest.raises(RuntimeError, match="no longer owned"):
        await communicate_fork(
            process,
            _request(_target("claude-code"), None, binary="unused"),
            asyncio.Event(),
            on_tool_executor=rejected,
            spawned_at=0.0,
        )
    assert process.returncode is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("through_adapter", [False, True])
async def test_cc_cutoff_drains_partial_stdout_and_never_releases_oversized_result(
    monkeypatch, through_adapter, isolated_db,
):
    seed_vendor_checkpoint_thread([_target("claude-code")])
    from core.fork_cc_io import communicate_fork
    from core.fork_types import ProviderForkRejected

    records = [
        {"type": "assistant", "message": {
            "id": "response", "stop_reason": "tool_use",
            "content": [{"type": "text", "text": "Partial checkpoint."},
                        {"type": "tool_use", "id": "edit", "name": "edit", "input": {}}],
            "usage": {"input_tokens": 900, "output_tokens": 50},
        }},
    ]
    script = (
        "import sys,signal\n"
        "sys.stdin.buffer.read()\n"
        f"print({json.dumps(chr(10).join(json.dumps(record) for record in records))}, flush=True)\n"
        "signal.pause()\n"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-u", "-c", script,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    registered = asyncio.Event()
    executor = None

    async def register(run):
        nonlocal executor
        executor = run
        registered.set()

    async def prepare(*_args):
        return b"Write a checkpoint."

    async def execute():
        return " x" * 100

    monkeypatch.setattr("core.fork_cc_cold.prepare_cold_payload", prepare)
    target = replace(_target("claude-code"), context_window=1000,
                     transport_model="gpt-4", expected_reported_model="gpt-4")
    request = _request(target, None, binary="unused")
    if through_adapter:
        from core import fork_cc as cc

        async def spawn(*_args, **_kwargs):
            return process

        monkeypatch.setattr(cc, "_spawn_process", spawn)
        operation = asyncio.create_task(cc.invoke_fork(
            request, asyncio.Event(), on_tool_executor=register, kind=CHECKPOINT_KIND,
        ))
    else:
        from core.fork_account import admit_fork

        request = admit_fork(request)  # a paid write names its account: invoke_fork does this itself
        operation = asyncio.create_task(communicate_fork(
            process, request, asyncio.Event(), on_tool_executor=register, spawned_at=0.0,
        ))
    try:
        await asyncio.wait_for(registered.wait(), 2)
        with pytest.raises(ProviderForkRejected, match="context limit"):
            await asyncio.wait_for(executor(execute, "edit", "edit", {}), 3)
        assert process.returncode is not None  # Death precedes the withheld reply.
        result = await asyncio.wait_for(operation, 3)
        if through_adapter:
            assert result.diagnostic == "checkpoint_context_truncated"
            assert "Partial checkpoint." in result.raw_body
            assert result.usage == {
                "prompt_tokens": 900, "completion_tokens": 50,
                "cache_read_tokens": 0, "cache_creation_tokens": 0,
                "effective_input_tokens": 900, "total_tokens": 950,
            }
        else:
            assert result.truncated
            assert b"Partial checkpoint." in result.stdout
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
