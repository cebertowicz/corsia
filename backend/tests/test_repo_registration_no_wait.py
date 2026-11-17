"""Repo registration goes ahead at once, whatever agents are running.

Same real registry, process owner, add procedure and fence as
``test_repo_registration_scope``; file tools are stand-ins under the real tool names
so the registry classifies them as it does in production.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.test_repo_registration_scope import (  # noqa: F401 - shared fixture and helpers
    _call,
    _own_target,
    _register,
    _registered,
    env,
)
from tools.base import Tool


class _HeldFileTool(Tool):
    """A file tool under a real tool name that holds its call open until released."""

    def __init__(self, name: str) -> None:
        self._name = name
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Hold a file call open."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        self.entered.set()
        await self.release.wait()
        return "released"


async def _held_file_call(env, tool_name: str, thread_id: str, path: Path) -> tuple[_HeldFileTool, asyncio.Task]:
    tool = _HeldFileTool(tool_name)
    env["registry"].register(tool)
    task = asyncio.create_task(_call(env["registry"], thread_id, tool_name, {"path": str(path)}))
    entered = asyncio.ensure_future(tool.entered.wait())
    await asyncio.wait({task, entered}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
    assert entered.done() and not task.done(), task.result() if task.done() else "never entered"
    return tool, task


async def _finish(tool: _HeldFileTool, task: asyncio.Task) -> None:
    tool.release.set()
    assert await task == "released"


@pytest.mark.parametrize(
    ("tool_name", "where"),
    [
        ("read_file", "inside"), ("grep", "inside"), ("glob", "inside"),
        ("write_file", "inside"), ("edit_file", "inside"), ("write_file", "outside"),
    ],
)
@pytest.mark.asyncio
async def test_a_file_call_in_flight_does_not_hold_registration(env, tool_name, where):
    target = _own_target(env)
    path = target / "tracked.txt" if where == "inside" else env["alpha"] / "notes.md"
    tool, task = await _held_file_call(env, tool_name, "p-agent", path)
    try:
        result = await asyncio.wait_for(_register(env, target), 5)
        # Registered while the call is still open.
        assert not task.done()
    finally:
        await _finish(tool, task)
    assert "Registered repository 'app'" in result, result
    assert _registered(env) == 1

