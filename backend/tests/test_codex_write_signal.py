"""Codex launch reconciles the read-only native-sandbox signal with reality.

The Codex app-server process is not itself confined by CORSIA's Safehouse
gate, so its native filesystem sandbox is launched read-only and every
sanctioned write flows through the CORSIA dynamic tools. Codex still surfaces
that read-only sandbox to the model as a blanket "workspace is read-only"
note, which some models take at face value and use to abandon a write-bearing
task without ever trying their tools. These tests pin the two launch-seam
guards that stop that: a write-path clarification for write-capable read-only
sessions, and a fail-fast preflight when a task-bearing session has no write
tool at all.
"""

from __future__ import annotations

import pytest

from subagents.codex_app_server import CodexAppServerSession
from subagents.codex_app_server_errors import CodexWriteSurfaceError
from subagents.managed_launch import (
    CODEX_WRITE_PATH_DEVELOPER_INSTRUCTIONS,
    CODEX_WRITE_PATH_MARKER,
)


def _tool(name: str) -> dict:
    """Build a CORSIA registry (OpenAI-nested) dynamic tool spec."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} through CORSIA",
            "parameters": {"type": "object"},
        },
    }


def _session(**kw) -> CodexAppServerSession:
    kw.setdefault("binary", "codex")
    kw.setdefault("task", "Implement the approved change")
    return CodexAppServerSession(**kw)


def test_write_capable_read_only_session_gets_clarification() -> None:
    session = _session(dynamic_tools=[_tool("bash"), _tool("read_file")])

    instructions = session._effective_developer_instructions()

    assert instructions is not None
    assert CODEX_WRITE_PATH_MARKER in instructions
    assert "worktree_create" in instructions
    assert "git worktree add" not in instructions
    assert "read-only" in instructions


def test_workspace_write_session_is_left_untouched() -> None:
    # A truthful "you can write" signal needs no reconciliation.
    session = _session(
        dynamic_tools=[_tool("bash")],
        sandbox_mode="workspace-write",
    )

    assert session._effective_developer_instructions() is None


def test_non_write_capable_session_is_not_clarified() -> None:
    session = _session(dynamic_tools=[_tool("read_file"), _tool("grep")])

    assert session._is_write_capable() is False
    assert session._effective_developer_instructions() is None


def test_caller_instructions_are_preserved_and_clarified() -> None:
    session = _session(
        dynamic_tools=[_tool("write_file")],
        developer_instructions="House rules.",
    )

    instructions = session._effective_developer_instructions()

    assert instructions is not None
    assert instructions.startswith("House rules.")
    assert CODEX_WRITE_PATH_MARKER in instructions


def test_clarification_is_not_duplicated() -> None:
    session = _session(
        dynamic_tools=[_tool("edit_file")],
        developer_instructions=CODEX_WRITE_PATH_DEVELOPER_INSTRUCTIONS,
    )

    assert (
        session._effective_developer_instructions()
        == CODEX_WRITE_PATH_DEVELOPER_INSTRUCTIONS
    )


def test_control_only_and_taskless_sessions_are_exempt() -> None:
    control = _session(task=None, control_only=True, dynamic_tools=[])
    taskless = _session(task=None, dynamic_tools=[])

    # Neither raises: control sessions and discovery probes are not write work.
    control._assert_launch_write_surface()
    taskless._assert_launch_write_surface()
    assert control._effective_developer_instructions() is None


def test_task_bearing_session_without_write_tool_is_refused() -> None:
    session = _session(dynamic_tools=[_tool("read_file")])

    with pytest.raises(CodexWriteSurfaceError) as error:
        session._assert_launch_write_surface()

    assert "cannot write" in str(error.value)
    assert error.value.payload["dynamic_tools"] == ["read_file"]


async def test_thread_start_carries_clarification_for_write_session() -> None:
    calls: list[tuple[str, dict]] = []

    async def request(method: str, params: dict | None = None):
        calls.append((method, params or {}))
        return {"thread": {"id": "thread-x"}}

    session = _session(dynamic_tools=[_tool("bash")])
    session._request = request

    await session._start_or_resume_thread()

    assert len(calls) == 1
    method, params = calls[0]
    assert method == "thread/start"
    assert params["sandbox"] == "read-only"
    assert CODEX_WRITE_PATH_MARKER in params["developerInstructions"]


async def test_preflight_refuses_before_any_thread_start() -> None:
    calls: list[str] = []

    async def request(method: str, params: dict | None = None):
        calls.append(method)
        return {"thread": {"id": "thread-x"}}

    session = _session(dynamic_tools=[_tool("read_file")])
    session._request = request

    with pytest.raises(CodexWriteSurfaceError):
        await session._start_or_resume_thread()

    # Fail fast: the launch never reached the provider.
    assert calls == []
