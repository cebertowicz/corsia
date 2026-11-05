"""File tool project boundaries and reviewed access."""

from pathlib import Path
import unicodedata
from unittest.mock import AsyncMock

import pytest

from tools.filesystem_edit import EditFileTool, ListDirTool, MultiEditTool
from tools.filesystem_rw import ReadFileTool, WriteFileTool
from tools.glob import GlobTool
from tools.grep import GrepTool
from tools.project_fence import Fence, FencedRoot


def test_refusal_names_workspace_agent_folder(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.project_fence._event", lambda *args, **kwargs: None)
    private = tmp_path / "private"
    fence = Fence((FencedRoot(private),), "caller")
    path = private / "notes.txt"
    assert fence.refusal(path) == (
        f"`{path}` is in a workspace agent's private folder, outside your scope. "
        "Ask the CORSIA agent, or use `request_host_access` for reviewed temporary access."
    )


@pytest.mark.asyncio
async def test_read_file_refuses_fenced_project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    target = root / "secret.txt"
    target.write_text("secret")
    monkeypatch.setattr("tools.project_fence._event", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.sandbox_config.is_path_allowed", lambda *args, **kwargs: True)
    monkeypatch.setattr("tools.project_fence.fence_for_thread", lambda thread_id: Fence((FencedRoot(root, "project"),), thread_id))
    tool = ReadFileTool(workspace=tmp_path)
    result = await tool.execute(str(target), _thread_id="caller")
    assert "outside your scope" in result
    assert "     1\tsecret" not in result


TOOLS = (ReadFileTool, WriteFileTool, EditFileTool, MultiEditTool, ListDirTool)


def _params(tool_type, target):
    params = {"path": str(target)}
    if tool_type is WriteFileTool:
        params["content"] = "changed"
    elif tool_type is EditFileTool:
        params.update(old_string="original", new_string="changed")
    elif tool_type is MultiEditTool:
        params["edits"] = [{"old_string": "original", "new_string": "changed"}]
    return params


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_type", TOOLS)
@pytest.mark.parametrize("spelling", ["plain", "dotdot", "symlink", "nfd", "case"])
async def test_file_tool_refuses_fenced_path_spelling(tmp_path, monkeypatch, tool_type, spelling):
    root = tmp_path / "Café"
    root.mkdir()
    (root / "file.txt").write_text("original")
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    choices = {
        "plain": root,
        "dotdot": tmp_path / "link" / ".." / "Café",
        "symlink": link,
        "nfd": tmp_path / unicodedata.normalize("NFD", root.name),
        "case": tmp_path / root.name.upper(),
    }
    target = choices[spelling]
    if tool_type is not ListDirTool:
        target /= "file.txt"
    monkeypatch.setattr("tools.project_fence._event", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.sandbox_config.is_path_allowed", lambda *args, **kwargs: True)
    monkeypatch.setattr("tools.project_fence.fence_for_thread", lambda thread_id: Fence((FencedRoot(root, "private"),), thread_id))
    result = await tool_type(workspace=tmp_path).execute(**_params(tool_type, target), _thread_id="caller")
    assert "outside your scope" in result
    assert (root / "file.txt").read_text() == "original"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_type", TOOLS)
async def test_local_only_refusal_does_not_consume_permit(tmp_path, monkeypatch, tool_type):
    root = tmp_path / "private"
    root.mkdir()
    target = root if tool_type is ListDirTool else root / "file.txt"
    if tool_type is not ListDirTool:
        target.write_text("original")
    consume = AsyncMock()
    monkeypatch.setattr("tools.host_access_gate.consume_current_action_permit", consume)
    monkeypatch.setattr("tools.project_fence._event", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.project_fence.fence_for_thread", lambda thread_id: Fence((FencedRoot(root, "private", True),), thread_id))
    result = await tool_type(workspace=tmp_path).execute(**_params(tool_type, target), _thread_id="caller")
    assert "outside your scope" in result
    consume.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_type", TOOLS)
async def test_open_agent_keeps_file_access(tmp_path, monkeypatch, tool_type):
    from tools.filesystem_gate import _resolve_path_for_action

    root = tmp_path / "project"
    root.mkdir()
    target = root if tool_type is ListDirTool else root / "file.txt"
    if tool_type is not ListDirTool:
        target.write_text("original")
    monkeypatch.setattr("tools.sandbox_config.is_path_allowed", lambda *args, **kwargs: True)
    monkeypatch.setattr("tools.project_fence.fence_for_thread", lambda thread_id: Fence())
    tool = tool_type(workspace=tmp_path)
    result = await _resolve_path_for_action(tool=tool, tool_name=tool.name,
        params=_params(tool_type, target), path=str(target), workspace=tmp_path,
        allowed_dir=None, thread_id="corsia", project_id=None,
        for_write=tool_type in (WriteFileTool, EditFileTool, MultiEditTool))
    assert result.resolved == target.resolve()
    assert not result.reviewed


@pytest.mark.asyncio
async def test_no_lease_returns_project_refusal(tmp_path, monkeypatch):
    from tools import host_access_gate as gate

    root = tmp_path / "private"
    root.mkdir()
    target = root / "file.txt"
    fence = Fence((FencedRoot(root, "private"),), "caller")
    monkeypatch.setattr("tools.project_fence._event", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.host_path_policy.fence_for_thread", lambda thread_id: fence)
    monkeypatch.setattr(gate, "is_path_allowed", lambda *args, **kwargs: True)
    monkeypatch.setattr(gate, "_resolve_active_lease", AsyncMock(return_value=None))
    with pytest.raises(gate.HostAccessRequired, match="outside your scope"):
        await gate.authorize_host_action(thread_id="caller", project_id=None,
            tool=ReadFileTool(workspace=tmp_path), tool_name="read_file", params={"path": str(target)})


@pytest.mark.asyncio
@pytest.mark.parametrize("local_only", [False, True])
async def test_lease_admission_respects_project_fence(tmp_path, monkeypatch, local_only):
    from tools import host_access_gate as gate

    root = tmp_path / "project"
    root.mkdir()
    target = root / "file.txt"
    target.write_text("original")
    fence = Fence((FencedRoot(root, "private", local_only),), "caller")
    monkeypatch.setattr("tools.project_fence._event", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.project_fence.fence_for_thread", lambda thread_id: fence)
    monkeypatch.setattr("tools.host_path_policy.fence_for_thread", lambda thread_id: fence)
    monkeypatch.setattr("tools.sandbox_config.is_path_allowed", lambda *args, **kwargs: True)
    monkeypatch.setattr(gate, "is_path_allowed", lambda *args, **kwargs: True)
    lease = {"id": "lease", "revision": 1, "application_hash": "a" * 64, "state": "active"}
    reviews = []
    consumed = []

    async def active(_thread_id):
        return lease

    async def review(_lease_id, **kwargs):
        reviews.append(kwargs)
        return {"lease": lease, "review": {"lease_id": "lease", "lease_revision": 1,
            "thread_id": "caller", "tool_name": "read_file", "action_hash": kwargs["action_hash"],
            "verdict": "allow", "permit_id": "permit", "permit_expires_at": "2030-01-01T00:00:00+00:00"}}

    async def consume(*args, **kwargs):
        consumed.append((args, kwargs))
        if len(consumed) > 1:
            raise gate.HostAccessBlocked("permit already consumed")

    monkeypatch.setattr(gate, "_resolve_active_lease", active)
    monkeypatch.setattr(gate, "_review_host_action", review)
    monkeypatch.setattr(gate, "_consume_host_action_permit", consume)
    tool = ReadFileTool(workspace=tmp_path)
    params = {"path": str(target), "offset": None, "limit": None}
    if local_only:
        with pytest.raises(gate.HostAccessBlocked, match="Local only"):
            await gate.authorize_host_action(thread_id="caller", project_id=None, tool=tool, tool_name="read_file", params=params)
        assert not reviews
        assert not consumed
    else:
        authorization = await gate.authorize_host_action(thread_id="caller", project_id=None, tool=tool, tool_name="read_file", params=params)
        assert len(reviews) == 1
        with gate.host_action_permit_scope(authorization):
            assert "original" in await tool.execute(**params, _thread_id="caller")
            assert "permit already consumed" in await tool.execute(**params, _thread_id="caller")
        assert len(consumed) == 2


def test_confined_child_launch_checks_child_fence(tmp_path, monkeypatch):
    from subagents.cc_confinement import ConfinementRefused, require_readable_dirs

    root = tmp_path / "private"
    root.mkdir()
    monkeypatch.setattr("tools.sandbox_config.is_path_allowed", lambda *args, **kwargs: True)
    monkeypatch.setattr("tools.project_fence._event", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.project_fence.fence_for_thread", lambda thread_id: Fence((FencedRoot(root, "private"),), thread_id))
    with pytest.raises(ConfinementRefused, match="outside your scope"):
        require_readable_dirs([str(root)], None, "child")


def test_attachment_paths_obey_calling_thread_fence(tmp_path, monkeypatch):
    from connections.mail import attachments
    from connections.mail.email_document import DraftRefused
    from email.message import EmailMessage

    root = tmp_path / "project"
    root.mkdir()
    (root / "file.txt").write_text("original")
    monkeypatch.setattr(attachments, "working_root", lambda thread_id: tmp_path)
    monkeypatch.setattr("tools.sandbox_config.is_path_allowed", lambda *args, **kwargs: True)
    monkeypatch.setattr("tools.project_fence._event", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.project_fence.fence_for_thread", lambda thread_id: Fence((FencedRoot(root, "private"),), thread_id))
    with pytest.raises(DraftRefused, match="missing or unreadable"):
        attachments.inspect("caller", ["project/file.txt"], 100)
    with pytest.raises(DraftRefused, match="missing or unreadable"):
        attachments.add("caller", EmailMessage(), [{"path": "project/file.txt", "size": 8}])


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_type", TOOLS)
@pytest.mark.parametrize("root_kind", ["ordinary", "unnamed", "local_after_review"])
async def test_each_tool_reviewed_permit_respects_fence(tmp_path, monkeypatch, tool_type, root_kind):
    from tools import host_access_gate as gate
    from tools.filesystem_gate import _resolve_path_for_action

    root = tmp_path / "private"
    root.mkdir()
    target = root if tool_type is ListDirTool else root / "file.txt"
    if tool_type is not ListDirTool:
        target.write_text("original")
    name = None if root_kind == "unnamed" else "private"
    state = {"local": False}
    def caller_fence(thread_id):
        return Fence((FencedRoot(root, name, state["local"]),), thread_id)
    monkeypatch.setattr("tools.project_fence._event", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.project_fence.fence_for_thread", caller_fence)
    monkeypatch.setattr("tools.host_path_policy.fence_for_thread", caller_fence)
    monkeypatch.setattr(gate, "is_path_allowed", lambda *args, **kwargs: True)
    monkeypatch.setattr(gate, "check_repo_write_path", lambda *args, **kwargs: None)
    lease = {"id": "lease", "revision": 1, "application_hash": "a" * 64, "state": "active"}
    calls = []
    async def review(_lease_id, **kwargs):
        return {"lease": lease, "review": {"lease_id": "lease", "lease_revision": 1,
            "thread_id": "caller", "tool_name": kwargs["tool_name"], "action_hash": kwargs["action_hash"],
            "verdict": "allow", "permit_id": "permit", "permit_expires_at": "2030-01-01T00:00:00+00:00"}}
    async def consume(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise gate.HostAccessBlocked("permit already consumed")
    monkeypatch.setattr(gate, "_resolve_active_lease", AsyncMock(return_value=lease))
    monkeypatch.setattr(gate, "_review_host_action", review)
    monkeypatch.setattr(gate, "_consume_host_action_permit", consume)
    tool = tool_type(workspace=tmp_path)
    params = _params(tool_type, target)
    authorization = await gate.authorize_host_action(thread_id="caller", project_id=None,
        tool=tool, tool_name=tool.name, params=params)
    async def resolve():
        return await _resolve_path_for_action(tool=tool, tool_name=tool.name, params=params,
            path=str(target), workspace=tmp_path, allowed_dir=None, thread_id="caller",
            project_id=None, for_write=tool_type in (WriteFileTool, EditFileTool, MultiEditTool))
    with gate.host_action_permit_scope(authorization):
        if root_kind == "local_after_review":
            state["local"] = True
            with pytest.raises(PermissionError, match="Local only"):
                await resolve()
            assert not calls
            state["local"] = False
        result = await resolve()
        assert result.reviewed and result.resolved == target.resolve()
        with pytest.raises(gate.HostAccessBlocked, match="already consumed"):
            await resolve()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_type,pattern", [(GlobTool, "**/*.txt"), (GrepTool, "needle")])
async def test_search_prunes_fenced_project_and_reports_it(tmp_path, monkeypatch, tool_type, pattern):
    root = tmp_path / "project"
    root.mkdir()
    (root / "private.txt").write_text("needle")
    (tmp_path / "open.txt").write_text("needle")
    monkeypatch.setattr("tools.project_fence._event", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.sandbox_config.is_path_allowed", lambda *args, **kwargs: True)
    monkeypatch.setattr("tools.project_fence.fence_for_thread", lambda thread_id: Fence((FencedRoot(root, "project"),), thread_id))
    result = await tool_type(workspace=tmp_path).execute(pattern, _thread_id="caller")
    assert "open.txt" in result
    assert "private.txt" not in result
    assert "Other projects this agent can't read were left out." in result
