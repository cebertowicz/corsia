"""Fail-closed admission and bridge execution for confined Codex threads."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
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
    with pytest.raises(ConfinementRefused) as error:
        confinement.assert_tools([*definitions(), extra])
    assert str(error.value) == (
        "Couldn’t start this read-only agent on Codex: "
        "it couldn’t be limited to reading files. Choose another model."
    )


def test_missing_tool_refusal_has_same_message():
    with pytest.raises(ConfinementRefused) as error:
        confinement.assert_tools(definitions()[:-1])
    assert str(error.value) == (
        "Couldn’t start this read-only agent on Codex: "
        "it couldn’t be limited to reading files. Choose another model."
    )


@pytest.mark.parametrize("version", ["codex-cli 0.162.0", "codex-cli 0.166.0", ""])
@pytest.mark.asyncio
async def test_unknown_version_refuses_before_worker_or_auth_access(tmp_path, monkeypatch, version):
    binary = tmp_path / "fake-codex"
    binary.write_text(f"#!{sys.executable}\nprint({version!r})\n")
    binary.chmod(0o700)
    from infra import config
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(confinement, "confined_dirs", lambda *_args: ("/review",))
    session = CodexAppServerSession(binary=str(binary), task="review", corsia_thread_id="worker",
                                    dynamic_tools=definitions())
    monkeypatch.setattr(session, "build_env", lambda: pytest.fail("auth must not be projected"))
    with pytest.raises(ConfinementRefused) as error:
        await session.start()
    display_version = version.removeprefix("codex-cli ") or "(version unknown)"
    assert str(error.value) == (
        f"Couldn’t start this read-only agent on Codex {display_version}: "
        "read-only agents work only on Codex 0.162.1, 0.163.0, 0.164.0 and 0.165.0. "
        "Switch Codex to one of those, or choose another model."
    )
    assert session._proc is None


@pytest.mark.parametrize("version", sorted(confinement.CAPTURED_CODEX_VERSIONS))
@pytest.mark.asyncio
async def test_captured_versions_are_admitted(tmp_path, version):
    binary = tmp_path / "fake-codex"
    binary.write_text(f"#!{sys.executable}\nprint({version!r})\n")
    binary.chmod(0o700)
    await confinement.verify_version(str(binary))


@pytest.mark.asyncio
async def test_start_and_resume_use_only_confined_definitions(monkeypatch):
    session = CodexAppServerSession(binary="fake", task="review", dynamic_tools=definitions())
    session.confined = True
    request = AsyncMock(return_value={"thread": {"id": "provider-thread"}})
    monkeypatch.setattr(session, "_request", request)
    await session._start_or_resume_thread()
    method, params = request.call_args.args
    assert method == "thread/start" and params["environments"] == []
    assert {tool["name"] for tool in params["dynamicTools"]} == confinement.UNTRUSTED_READ_TOOLS
    assert params["sandbox"] == "read-only" and params["approvalPolicy"] == "never"
    await session._start_or_resume_thread()
    method, params = request.call_args.args
    assert method == "thread/resume" and "dynamicTools" not in params
    assert session.dynamic_tools == definitions()


def test_live_handle_cannot_restore_an_unconfined_surface():
    from subagents.codex_destination_render import _tool_surface
    from subagents.codex_runtime_errors import CodexPreparedContextStale

    context = True
    session = SimpleNamespace(tool_registry=object(), dynamic_tools=definitions(), confined=False)
    with pytest.raises(CodexPreparedContextStale, match="confined live handle"):
        _tool_surface("/empty", session, context)
    session.confined = True
    session.dynamic_tools = (*definitions(), {"type": "function", "function": {"name": "bash"}})
    with pytest.raises(ConfinementRefused):
        _tool_surface("/empty", session, context)


@pytest.mark.asyncio
async def test_confined_fork_refuses_and_cleans_its_home(isolated_db, tmp_path, monkeypatch):
    from dataclasses import replace
    from connections.account_runtime import LaunchHomes
    from core import fork_codex
    from core.checkpoint_fork_kind import CHECKPOINT_KIND
    from core.fork_fit import ColdFoldInput
    from core.fork_types import ProviderForkRejected
    from test_checkpoint_provider_adapters import _request, _target
    from test_confined_reads import _seed_thread

    _seed_thread(isolated_db, "thread-1", tmp_path)
    request = replace(_request(None), target=_target("codex"), binary="unused",
                      working_dir=str(tmp_path), vendor_launch=LaunchHomes(None, False, warm_home=str(tmp_path)))
    monkeypatch.setattr(fork_codex, "admit_fork", lambda value: value)
    monkeypatch.setattr(fork_codex, "_fit_cold_request", lambda value, *a, **k: (value, ColdFoldInput((), 1, None, 0)))
    monkeypatch.setattr(fork_codex, "_fork_tools", lambda *a, **k: (object(), ()))
    monkeypatch.setattr(fork_codex, "bind_fork_session", lambda _request, session, **kw: session)
    original = confinement.prepare_launch
    homes = []
    def check_launch(session):
        homes.append(session.codex_home)
        assert Path(session.codex_home).is_dir()
        return original(session)
    monkeypatch.setattr(confinement, "prepare_launch", check_launch)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=AssertionError("process spawned")))
    with pytest.raises(ProviderForkRejected, match="can’t fork"):
        await fork_codex.invoke_fork(request, asyncio.Event(), kind=CHECKPOINT_KIND)
    assert len(homes) == 1 and not Path(homes[0]).exists()


@pytest.mark.parametrize("attribute,value", [("control_only", True),
                                             ("sandbox_mode", "workspace-write"), ("approval_policy", "on-request")])
def test_confined_launch_refuses_control_fork_and_weakened_policy(tmp_path, monkeypatch, attribute, value):
    monkeypatch.setattr(confinement, "confined_dirs", lambda *_args: ("/review",))
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
            assert "not available to a confined untrusted worker" in result
        for path in (auth, grant / "auth-link"):
            result = await registry.execute("read_file", {"path": str(path)})
            assert "outside this confined worker's read paths" in result
            assert "AUTH_STAND_IN" not in result
        assert "REVIEW_INPUT" in await registry.execute("read_file", {"path": str(grant / "allowed.md")})
    finally:
        state._current_thread_id.reset(marker)
