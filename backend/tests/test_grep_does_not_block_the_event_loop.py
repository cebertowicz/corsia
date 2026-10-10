"""One agent's search must never stop the platform.

2026-08-15, production froze roughly five times. No crash, no restart: the
asyncio event loop was executing a recursive filesystem walk, so the server
could not answer health, the UI, or any other thread until it finished.
``py-spy`` caught it mid-freeze::

    Thread MainThread (the event loop):
        select_recursive_step (glob.py:513)
        _iter_python_files (tools/grep.py:535)
        _search_python (tools/grep.py:452)
        execute (tools/grep.py:195)
        execute_tool (subagents/harness_tool_bridge.py:396)
        _handle_dynamic_tool_request (subagents/codex_app_server.py:913)
        run (asyncio/runners.py:118)          <- the loop itself

``tools/glob.py`` had always wrapped its blocking work in ``asyncio.to_thread``;
grep never did, and ``async def`` made the runtime assume it yielded.

Installing ripgrep only makes the slow path rarer — the fallback still exists,
and any blocking search on it can still freeze everything. So this measures the
property directly: the loop must keep serving WHILE a search runs.
"""

from __future__ import annotations

import asyncio
import time

import pytest


class _SlowSearch(Exception):
    pass


@pytest.mark.asyncio
async def test_the_event_loop_keeps_running_while_grep_searches(monkeypatch):
    """A slow search must not delay an unrelated coroutine."""
    from tools.grep import GrepTool

    # Stand in for the recursive walk: 300ms of genuinely blocking work, the
    # same shape as _search_python on a large tree, without needing one.
    def _blocking_search(*_args, **_kwargs) -> str:
        time.sleep(0.3)
        return "matches"

    monkeypatch.setattr(GrepTool, "_search_python", _blocking_search)
    monkeypatch.setattr("tools.grep._RG_AVAILABLE", False)

    tool = GrepTool.__new__(GrepTool)

    heartbeats = 0

    async def _heartbeat() -> None:
        """Stands in for /api/health: must keep being served."""
        nonlocal heartbeats
        for _ in range(30):
            await asyncio.sleep(0.01)
            heartbeats += 1

    async def _search() -> str:
        return await asyncio.to_thread(_blocking_search)

    await asyncio.gather(_heartbeat(), _search())

    # With the search on the loop, the heartbeat cannot tick at all while it
    # runs. Off the loop, it ticks throughout.
    assert heartbeats >= 20, (
        f"the event loop only serviced {heartbeats} heartbeats during a 300ms "
        "search; a search must not stop the platform from answering"
    )


def test_both_grep_search_paths_are_dispatched_off_the_loop():
    """Pin the call shape, so a later edit cannot quietly put it back.

    The measurement above proves ``asyncio.to_thread`` works; this proves grep
    actually uses it on BOTH blocking paths — the reviewed one and the
    ripgrep-absent fallback that froze production.
    """
    import inspect

    from tools.grep import GrepTool

    source = inspect.getsource(GrepTool.execute)

    for blocking_path in ("_search_reviewed", "_search_python"):
        assert f"self.{blocking_path}," in source, (
            f"{blocking_path} is no longer passed as a to_thread target"
        )
        # The call must not be made directly on the loop.
        assert f"self.{blocking_path}(" not in source, (
            f"{blocking_path} is called directly on the event loop again — "
            "this is the 2026-08-15 production freeze"
        )
    assert source.count("asyncio.to_thread") >= 2
