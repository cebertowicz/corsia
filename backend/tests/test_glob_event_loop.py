from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("reviewed", [False, True])
async def test_glob_traversal_does_not_block_the_event_loop(
    monkeypatch,
    tmp_path,
    reviewed,
) -> None:
    """A broad filesystem walk must not make HTTP and WebSocket work stall."""
    from tools import glob as glob_module

    release = threading.Event()
    entered = threading.Event()

    async def authorize(**_kwargs):
        return SimpleNamespace(resolved=tmp_path, reviewed=reviewed)

    def slow_result(*_args, **_kwargs):
        entered.set()
        release.wait(timeout=1.0)
        return "finished"

    def slow_matches(*_args, **_kwargs):
        entered.set()
        release.wait(timeout=1.0)
        return iter(())

    monkeypatch.setattr(glob_module, "_resolve_path_for_action", authorize)
    if reviewed:
        monkeypatch.setattr(glob_module.GlobTool, "_glob_reviewed", slow_result)
    else:
        monkeypatch.setattr(glob_module, "select_matches", slow_matches)

    timer = threading.Timer(0.3, release.set)
    timer.start()
    try:
        task = asyncio.create_task(
            glob_module.GlobTool(workspace=tmp_path).execute("**/*")
        )
        assert await asyncio.to_thread(entered.wait, 0.2)
        await asyncio.sleep(0.05)
        assert not release.is_set(), "glob traversal blocked the event loop"
        release.set()
        await task
    finally:
        release.set()
        timer.cancel()

