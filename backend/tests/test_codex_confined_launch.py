"""Fail-closed admission and bridge execution for confined Codex threads."""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from subagents import codex_confinement as confinement
from subagents.cc_confinement import ConfinementRefused
from subagents.codex_app_server import CodexAppServerSession
from test_codex_confined_capture import definitions


@pytest.mark.parametrize("extra", [
    {"type": "function", "name": "exec_command"},
    {"type": "custom", "name": "apply_patch"},
    {"type": "function", "name": "view_image"},
    {"type": "web_search"}, {"type": "image_generation"},
    {"type": "function", "name": "clock"},
])
def test_full_surface_refuses_any_extra_native_or_hosted_tool(extra):
    with pytest.raises(ConfinementRefused, match="update CORSIA"):
        confinement.assert_tools([*definitions(), extra])


@pytest.mark.parametrize("version", ["codex-cli 0.162.0", "codex-cli 0.163.0", ""])
@pytest.mark.asyncio
async def test_unknown_version_refuses_before_worker_or_auth_access(tmp_path, monkeypatch, version):
    binary = tmp_path / "fake-codex"
    binary.write_text(f"#!{sys.executable}\nprint({version!r})\n")
    binary.chmod(0o700)
    from infra import config
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(confinement, "thread_context", lambda *_args: {
        "confinement": "review", "confined_read_dirs": '["/review"]',
    })
    session = CodexAppServerSession(binary=str(binary), task="review", corsia_thread_id="worker",
                                    dynamic_tools=definitions())
    monkeypatch.setattr(session, "build_env", lambda: pytest.fail("auth must not be projected"))
    with pytest.raises(ConfinementRefused, match="update CORSIA"):
        await session.start()
    assert session._proc is None


@pytest.mark.asyncio
async def test_start_and_resume_use_only_confined_definitions(monkeypatch):
    session = CodexAppServerSession(binary="fake", task="review", dynamic_tools=definitions())
    session.confined = True
    request = AsyncMock(return_value={"thread": {"id": "provider-thread"}})
    monkeypatch.setattr(session, "_request", request)
    await session._start_or_resume_thread()
    method, params = request.call_args.args
    assert method == "thread/start" and params["environments"] == []
    assert {tool["name"] for tool in params["dynamicTools"]} == confinement.REVIEW_READ_TOOLS
    assert params["sandbox"] == "read-only" and params["approvalPolicy"] == "never"
    await session._start_or_resume_thread()
    method, params = request.call_args.args
    assert method == "thread/resume" and "dynamicTools" not in params
    assert session.dynamic_tools == definitions()


def test_live_handle_cannot_restore_an_unconfined_surface():
    from subagents.codex_destination_render import _tool_surface
    from subagents.codex_runtime_errors import CodexPreparedContextStale

    context = {"confinement": "review", "confined_read_dirs": '["/review"]'}
    session = SimpleNamespace(tool_registry=object(), dynamic_tools=definitions(), confined=False)
    with pytest.raises(CodexPreparedContextStale, match="confined live handle"):
        _tool_surface("/empty", session, context)
    session.confined = True
    session.dynamic_tools = (*definitions(), {"type": "function", "function": {"name": "bash"}})
    with pytest.raises(ConfinementRefused):
        _tool_surface("/empty", session, context)


@pytest.mark.asyncio
async def test_confined_fork_refuses_before_admission(monkeypatch):
    from core import fork_codex
    from core.fork_types import ProviderForkRejected

    monkeypatch.setattr(confinement, "thread_context", lambda *_args: {"confinement": "review"})
    monkeypatch.setattr(fork_codex, "admit_fork", lambda *_args: pytest.fail("fork must not admit"))
    request = SimpleNamespace(thread_id="worker", target=SimpleNamespace(harness="codex"))
    with pytest.raises(ProviderForkRejected, match="cannot fork"):
        await fork_codex.invoke_fork(request, asyncio.Event(), kind=SimpleNamespace(name="review"))


@pytest.mark.parametrize("attribute,value", [("control_only", True), ("fork_id", "fork"),
                                             ("sandbox_mode", "workspace-write"), ("approval_policy", "on-request")])
def test_confined_launch_refuses_control_fork_and_weakened_policy(tmp_path, monkeypatch, attribute, value):
    monkeypatch.setattr(confinement, "thread_context", lambda *_args: {"confinement": "review"})
    session = CodexAppServerSession(binary="fake", task="review", dynamic_tools=definitions())
    setattr(session, attribute, value)
    with pytest.raises(ConfinementRefused):
        confinement.prepare_launch(session)


@pytest.mark.asyncio
async def test_codex_bridge_refuses_commands_web_auth_home_and_symlink(isolated_db, tmp_path, monkeypatch):
    from infra import state
    from test_confined_reads import _configure_sandbox, _seed_thread
    from tools.filesystem_rw import ReadFileTool
    from tools.registry import ToolRegistry
    from tools.shell import BashTool
    from tools.base import Tool

    grant = tmp_path / "stage"
    grant.mkdir()
    auth_home = tmp_path / "isolated-codex"
    auth_home.mkdir()
    auth = auth_home / "auth.json"
    auth.write_text("AUTH_STAND_IN_MUST_NOT_REACH_MODEL")
    (grant / "auth-link").symlink_to(auth)
    (grant / "allowed.md").write_text("REVIEW_INPUT")
    _configure_sandbox(monkeypatch, tmp_path)
    _seed_thread(isolated_db, "worker", grant)

    class WebTool(Tool):
        name = "web_search"
        description = "Web search stand-in"
        parameters = {"type": "object", "properties": {}}
        async def execute(self, **_kwargs):
            pytest.fail("web tool must not execute")

    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace=tmp_path))
    registry.register(BashTool(working_dir=str(tmp_path)))
    registry.register(WebTool())
    marker = state._current_thread_id.set("worker")
    try:
        for name, params in (("bash", {"command": "id"}), ("web_search", {})):
            result = await registry.execute(name, params)
            assert "not available to a confined review worker" in result
        for path in (auth, grant / "auth-link"):
            result = await registry.execute("read_file", {"path": str(path)})
            assert "outside this confined worker's read paths" in result
            assert "AUTH_STAND_IN" not in result
        assert "REVIEW_INPUT" in await registry.execute("read_file", {"path": str(grant / "allowed.md")})
    finally:
        state._current_thread_id.reset(marker)
