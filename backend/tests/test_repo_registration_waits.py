"""Repo registration: only writes inside the folder conflict, and a conflict is waited out.

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
    _held_effect,
    _own_target,
    _register,
    _registered,
    _release,
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


@pytest.mark.parametrize("tool_name", ["read_file", "grep", "glob"])
@pytest.mark.asyncio
async def test_a_read_inside_the_folder_does_not_block(env, tool_name):
    target = _own_target(env)
    tool, task = await _held_file_call(env, tool_name, "p-agent", target / "tracked.txt")
    try:
        result = await asyncio.wait_for(_register(env, target), 5)
    finally:
        await _finish(tool, task)
    assert "Registered repository 'app'" in result, result
    assert _registered(env) == 1


@pytest.mark.asyncio
async def test_a_write_outside_the_folder_does_not_block(env):
    target = _own_target(env)
    tool, task = await _held_file_call(env, "write_file", "p-agent", env["alpha"] / "notes.md")
    try:
        result = await asyncio.wait_for(_register(env, target), 5)
    finally:
        await _finish(tool, task)
    assert "Registered repository 'app'" in result, result


@pytest.mark.parametrize("tool_name", ["write_file", "edit_file"])
@pytest.mark.asyncio
async def test_a_write_inside_the_folder_is_waited_out(env, tool_name):
    target = _own_target(env)
    tool, task = await _held_file_call(env, tool_name, "p-agent", target / "tracked.txt")
    registering = asyncio.create_task(_register(env, target))
    try:
        await asyncio.sleep(0.3)
        # Still waiting, and the loop is free: this coroutine ran while it waits.
        assert not registering.done()
        assert _registered(env) == 0
    finally:
        await _finish(tool, task)
    result = await asyncio.wait_for(registering, 5)
    assert "Registered repository 'app'" in result, result
    assert _registered(env) == 1


@pytest.mark.asyncio
async def test_an_action_that_could_write_the_folder_is_waited_out(env):
    """A call whose write paths aren't known counts by its caller's reach, then is waited out."""
    target = _own_target(env)
    task = await _held_effect(env, "p-agent")
    registering = asyncio.create_task(_register(env, target))
    try:
        await asyncio.sleep(0.3)
        assert not registering.done()
    finally:
        await _release(env, task)
    result = await asyncio.wait_for(registering, 5)
    assert "Registered repository 'app'" in result, result


@pytest.mark.asyncio
async def test_a_write_that_outlasts_the_wait_is_refused_and_named(env):
    target = _own_target(env)
    tool, task = await _held_file_call(env, "write_file", "p-agent", target / "tracked.txt")
    try:
        result = await _register(env, target)
    finally:
        await _finish(tool, task)
    assert "while agent \"Alpha worker\" is running an action." in result, result
    assert "Waited 1 minute for it." in result, result
    assert _registered(env) == 0


@pytest.mark.asyncio
async def test_a_refusal_that_waiting_cannot_end_is_raised_at_once():
    from infra.repo_policy_transition import RepoPolicyTransitionBusy, wait_for_transition

    attempts = 0

    async def attempt():
        nonlocal attempts
        attempts += 1
        raise RepoPolicyTransitionBusy("Owned process activity could not be verified.")

    with pytest.raises(RepoPolicyTransitionBusy, match="could not be verified.$"):
        await wait_for_transition(attempt, wait_seconds=30)
    assert attempts == 1


@pytest.mark.asyncio
async def test_the_wait_retries_until_the_conflict_ends():
    from infra.repo_policy_transition import RepoPolicyTransitionBusy, wait_for_transition

    attempts = 0

    async def attempt():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RepoPolicyTransitionBusy("busy", retryable=True)
        return "done"

    assert await wait_for_transition(attempt, wait_seconds=30) == "done"
    assert attempts == 3
