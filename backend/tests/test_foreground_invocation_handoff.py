"""Invocation binding through real entry points; no provider or external effects."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from infra.state.context import _current_tool_invocation_id, tool_invocation_context
from tools.registry import _is_reserved_execution_param
from tools.tool_execution_context import inject_context, strip_reserved_context
from tools.shell_guard import safehouse_subprocess_env


@pytest.mark.asyncio
@pytest.mark.parametrize("invocation,checkpoint,expected", [
    ("mcp-call", None, "mcp-call"),
    ("provider-call", None, "provider-call"),
    (None, None, None),
    ("", None, None),
    ("private-call", "fork", None),
])
async def test_bridge_binds_only_current_invocation(monkeypatch, invocation, checkpoint, expected):
    from subagents import harness_tool_bridge as bridge

    observed = []

    class Registry:
        async def execute(self, _name, _arguments):
            observed.append(_current_tool_invocation_id.get())
            return "executed"

        def get(self, _name):
            return None

    monkeypatch.setattr(bridge, "publish_completed_tool_result", AsyncMock(return_value="published"))
    with tool_invocation_context("outer"):
        result = await bridge.execute_tool(
            Registry(), thread_id="thread", invocation_id=invocation,
            provider_tool_call_id="must-not-be-used", checkpoint_id=checkpoint,
            name="bash", arguments={"command": "fake"},
        )
        assert _current_tool_invocation_id.get() == "outer"
    assert result == "published"
    assert observed == [expected]


@pytest.mark.asyncio
async def test_bridge_concurrent_calls_do_not_share_invocations(monkeypatch):
    from subagents import harness_tool_bridge as bridge

    entered = asyncio.Event()
    release = asyncio.Event()
    observed = []

    class Registry:
        async def execute(self, _name, arguments):
            if arguments["slow"]:
                entered.set()
                await release.wait()
            observed.append(_current_tool_invocation_id.get())
            return "executed"

        def get(self, _name):
            return None

    monkeypatch.setattr(bridge, "publish_completed_tool_result", AsyncMock(return_value="published"))
    registry = Registry()
    slow = asyncio.create_task(bridge.execute_tool(
        registry, thread_id="one", invocation_id="one-call",
        name="bash", arguments={"slow": True},
    ))
    await entered.wait()
    await bridge.execute_tool(
        registry, thread_id="two", invocation_id="two-call",
        name="bash", arguments={"slow": False},
    )
    release.set()
    await slow
    assert observed == ["two-call", "one-call"]
    assert _current_tool_invocation_id.get() is None


@pytest.mark.asyncio
async def test_native_call_binds_its_request_id_and_restores_after_failure():
    from core.turns_steps import _execute_tool_calls_inner

    observed = []

    class Tools:
        async def execute(self, _name, _arguments):
            observed.append(_current_tool_invocation_id.get())
            raise RuntimeError("fixture failure")

    agent = SimpleNamespace(
        tools=Tools(), _strip_think=lambda text: text,
        _tool_hint=lambda calls: calls[0].name,
        context=SimpleNamespace(add_assistant_message=lambda messages, *_args, **_kwargs: messages),
    )
    response = SimpleNamespace(
        content="", reasoning_content=None,
        tool_calls=[SimpleNamespace(id="native-call", name="bash", arguments={})],
    )
    with tool_invocation_context("outer"):
        with pytest.raises(RuntimeError, match="fixture failure"):
            await _execute_tool_calls_inner(
                agent, response, [], "thread", asyncio.Event(), AsyncMock(), [], "model",
            )
        assert _current_tool_invocation_id.get() == "outer"
    assert observed == ["native-call"]


@pytest.mark.asyncio
async def test_checkpoint_native_clears_inherited_foreground_invocation(monkeypatch):
    from core import checkpoint_native_tools as checkpoint

    observed = []

    class Tools:
        async def execute(self, _name, _arguments):
            observed.append(_current_tool_invocation_id.get())
            return "staged"

    monkeypatch.setattr(checkpoint, "input_fits_off_loop", AsyncMock(return_value=True))
    response = SimpleNamespace(
        content="", reasoning_content=None,
        tool_calls=[SimpleNamespace(id="fork-call", name="continuity_edit", arguments={})],
    )
    request = SimpleNamespace(checkpoint_id="fork", thread_id="thread")
    with tool_invocation_context("outer"):
        assert await checkpoint.run_tool_calls(
            SimpleNamespace(tools=Tools()), response, [], asyncio.Event(), request, [],
        )
        assert _current_tool_invocation_id.get() == "outer"
    assert observed == [None]


def test_registry_forwards_invocation_only_to_bash_and_strips_public_override():
    params = strip_reserved_context({
        "command": "fake", "_thread_invocation_id": "forged",
    }, _is_reserved_execution_param)
    context = {"id": "thread", "project_id": "project"}
    with tool_invocation_context("current"):
        forwarded = inject_context(params, context, tool_name="bash")
        other = inject_context(params, context, tool_name="read_file")
    assert forwarded["_thread_invocation_id"] == "current"
    assert "_thread_invocation_id" not in other
    assert "_thread_invocation_id" not in params


@pytest.mark.parametrize("current,expected", [(None, None), ("", None), ("current", "current")])
def test_shell_environment_discards_inherited_invocation(monkeypatch, current, expected):
    monkeypatch.setenv("CORSIA_TOOL_INVOCATION_ID", "inherited-forgery")
    env = safehouse_subprocess_env("thread", invocation_id=current)
    assert env.get("CORSIA_TOOL_INVOCATION_ID") == expected
    assert safehouse_subprocess_env(None, invocation_id="current").get("CORSIA_TOOL_INVOCATION_ID") is None


@pytest.mark.parametrize("current,expected", [("current-call", "current-call"), (None, "unset")])
def test_real_safehouse_forwards_only_bound_invocation(monkeypatch, tmp_path, current, expected):
    import subprocess

    from tools import shell_guard

    safehouse = shell_guard.shutil.which("safehouse")
    reason = None
    if safehouse is None:
        reason = "Agent Safehouse is unavailable; jailed export remains unverified"
    else:
        version = subprocess.run(
            [safehouse, "--version"], capture_output=True, text=True, timeout=10,
        )
        if version.returncode != 0 or version.stdout.strip() != "Agent Safehouse 0.11.1":
            reason = "Jailed export proof requires Agent Safehouse 0.11.1"
    if reason is not None:
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail(reason + "; CI requires both jailed export cases to execute")
        pytest.skip(reason)
    monkeypatch.setenv("CORSIA_TOOL_INVOCATION_ID", "inherited-forgery")
    monkeypatch.setattr(shell_guard, "list_registered_repo_policies", lambda: [])
    monkeypatch.setattr(shell_guard, "get_allowed_dirs_rw", lambda: [tmp_path])
    monkeypatch.setattr(shell_guard, "get_sandbox_add_dirs_rw", lambda _thread_id, policies=None: [tmp_path])
    monkeypatch.setattr(shell_guard, "get_allowed_dirs_ro", lambda policies=None: [])
    command = "printf '%s' \"${CORSIA_TOOL_INVOCATION_ID-unset}\""
    profile = tmp_path / "invocation-proof.sb"
    profile.write_text(shell_guard.execution_envelope_profile(
        command, tmp_path, profile, leased=False,
    ), encoding="utf-8")
    result = subprocess.run(
        shell_guard.safehouse_argv(command, tmp_path, profile),
        cwd=tmp_path, env=safehouse_subprocess_env("thread", invocation_id=current),
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected
