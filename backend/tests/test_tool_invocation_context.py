"""Prototype tests of the shared invocation scope, not shell transport proof."""
import asyncio

import pytest

from infra.state.context import _current_tool_invocation_id, tool_invocation_context


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_missing_invocation_clears_an_inherited_foreground_identity(missing):
    with tool_invocation_context("outer-call"):
        with tool_invocation_context(missing):
            assert _current_tool_invocation_id.get() is None
        assert _current_tool_invocation_id.get() == "outer-call"
    assert _current_tool_invocation_id.get() is None


def test_checkpoint_scope_never_inherits_or_exports_a_foreground_call():
    with tool_invocation_context("foreground-call"):
        with tool_invocation_context("private-call", checkpoint_id="checkpoint-1"):
            assert _current_tool_invocation_id.get() is None
        assert _current_tool_invocation_id.get() == "foreground-call"
    assert _current_tool_invocation_id.get() is None


@pytest.mark.asyncio
async def test_parallel_calls_keep_exact_separate_identities_and_restore_parent():
    arrived = 0
    both_arrived = asyncio.Event()

    async def execute(identity):
        nonlocal arrived
        with tool_invocation_context(identity):
            arrived += 1
            if arrived == 2:
                both_arrived.set()
            await both_arrived.wait()
            await asyncio.sleep(0)
            return _current_tool_invocation_id.get()

    with tool_invocation_context("parent-call"):
        values = await asyncio.gather(execute("call-a"), execute("call-b"))
        assert values == ["call-a", "call-b"]
        assert _current_tool_invocation_id.get() == "parent-call"
    assert _current_tool_invocation_id.get() is None


@pytest.mark.asyncio
async def test_cancelled_execution_restores_the_context_before_error_returns():
    executing = asyncio.Event()
    restored = []

    async def execute():
        try:
            with tool_invocation_context("cancelled-call"):
                executing.set()
                await asyncio.Event().wait()
        finally:
            restored.append(_current_tool_invocation_id.get())

    with tool_invocation_context("parent-call"):
        child = asyncio.create_task(execute())
        await executing.wait()
        child.cancel()
        with pytest.raises(asyncio.CancelledError):
            await child
        assert restored == ["parent-call"]
        assert _current_tool_invocation_id.get() == "parent-call"
    assert _current_tool_invocation_id.get() is None
