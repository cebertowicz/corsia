from __future__ import annotations

import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


def _configure_sandbox(monkeypatch, workspace: Path, policies=()) -> None:
    from tools import sandbox_config

    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [workspace])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(
        sandbox_config,
        "_list_authoritative_repo_policies",
        lambda **_filters: list(policies),
    )


def test_descriptor_binding_fails_closed_when_platform_support_is_missing(
    tmp_path,
    monkeypatch,
) -> None:
    from tools import filesystem_effects

    target = tmp_path / "notes.txt"
    target.write_text("notes\n", encoding="utf-8")
    monkeypatch.setattr(filesystem_effects, "_NOFOLLOW", 0)

    with pytest.raises(
        filesystem_effects.FilesystemBindingError,
        match="cannot safely bind",
    ):
        with filesystem_effects.open_bound_text(target):
            pass


def test_reviewed_glob_matching_is_root_relative() -> None:
    from tools.filesystem_effects import match_relative_glob

    assert match_relative_glob(Path("top.py"), "**/*.py")
    assert match_relative_glob(Path("nested/deep.py"), "**/*.py")
    assert match_relative_glob(Path("nested/deep.py"), "nested/*.py")
    assert match_relative_glob(Path("component.ts"), "**/*.{ts,tsx}")
    assert match_relative_glob(Path("nested/component.tsx"), "**/*.{ts,tsx}")
    assert not match_relative_glob(Path("nested/deep.py"), "*.py")
    assert not match_relative_glob(Path("other/deep.py"), "nested/*.py")


def test_bound_walk_never_descends_into_a_credential_root(tmp_path, monkeypatch) -> None:
    from infra import config
    from tools.filesystem_effects import walk_bound_files

    state_dir = tmp_path / "state"
    (state_dir / "keys").mkdir(parents=True)
    (state_dir / "keys" / "values.json").write_text("k\n", encoding="utf-8")
    (state_dir / "corsia.db").write_text("d\n", encoding="utf-8")
    monkeypatch.setattr(config, "STATE_ROOT", state_dir)

    for root in (state_dir, tmp_path):
        with walk_bound_files(root, skip_directory=lambda _relative: False) as files:
            names = {entry.relative.name for entry in files}
        assert "values.json" not in names
        assert "corsia.db" in names


def test_bound_search_closes_nested_descriptors_after_early_exit(tmp_path) -> None:
    from tools.filesystem_effects import walk_bound_files

    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "notes.txt").write_text("notes\n", encoding="utf-8")

    with walk_bound_files(tmp_path) as files:
        entry = next(files)
        nested_parent_fd = entry._parent_fd
        os.fstat(nested_parent_fd)

    with pytest.raises(OSError):
        os.fstat(nested_parent_fd)


@pytest.mark.asyncio
async def test_protected_read_reaches_lower_gate_then_uses_ordinary_path(
    tmp_path,
    monkeypatch,
):
    from tools import host_access_gate
    from tools.filesystem_rw import ReadFileTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("protected\n", encoding="utf-8")
    _configure_sandbox(monkeypatch, workspace)
    calls = []

    async def consume(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )

    result = await ReadFileTool(workspace=workspace).execute(
        path="notes.txt",
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )

    assert "protected" in result
    assert len(calls) == 1
    assert calls[0]["thread_id"] == "thread-1"
    assert calls[0]["project_id"] == "project-1"
    assert calls[0]["tool_name"] == "read_file"
    assert calls[0]["params"] == {
        "path": "notes.txt",
        "offset": None,
        "limit": None,
    }


@pytest.mark.asyncio
async def test_state_root_requires_review_but_consumed_permit_can_read(
    tmp_path,
    monkeypatch,
):
    from infra import config
    from tools import host_access_gate
    from tools.filesystem_rw import ReadFileTool

    workspace = tmp_path / "workspace"
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    target = state_dir / "state.db"
    target.write_text("reviewed-state\n", encoding="utf-8")
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "STATE_ROOT", state_dir)
    _configure_sandbox(monkeypatch, workspace)

    async def no_permit(**_kwargs):
        return None

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        no_permit,
    )
    ordinary = await ReadFileTool(workspace=workspace).execute(
        path=str(target),
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )
    assert "requires reviewed host access" in ordinary

    async def reviewed(**_kwargs):
        return SimpleNamespace(resolved_path=target.resolve(strict=False))

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        reviewed,
    )
    elevated = await ReadFileTool(workspace=workspace).execute(
        path=str(target),
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )
    assert "reviewed-state" in elevated


@pytest.mark.asyncio
@pytest.mark.parametrize("write", [False, True])
@pytest.mark.parametrize("floor", ["keys/values.json", "keys/new.json"])
async def test_key_store_is_refused_with_a_permit_and_leaves_it_unconsumed(
    floor,
    write,
    tmp_path,
    monkeypatch,
):
    from infra import config
    from tools import host_access_gate as gate
    from tools.filesystem_rw import ReadFileTool, WriteFileTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    target = state_dir / floor
    target.parent.mkdir(parents=True)
    target.write_text("private-material\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "STATE_ROOT", state_dir)
    _configure_sandbox(monkeypatch, workspace)
    consumed = []

    async def consume_permit(*args, **kwargs):
        consumed.append((args, kwargs))

    monkeypatch.setattr(gate, "_consume_host_action_permit", consume_permit)
    permit = gate._ActionPermit(
        permit_id="permit-1",
        lease_id="lease-1",
        thread_id="thread-1",
        lease_revision=1,
        application_hash="hash",
        action=SimpleNamespace(tool_name="write_file" if write else "read_file", action_hash="x"),
    )
    token = gate._current_action_permit.set(permit)
    try:
        if write:
            result = await WriteFileTool(workspace=workspace).execute(
                path=str(target),
                content="replaced\n",
                description="test output",
                _thread_id="thread-1",
                _thread_project_id="project-1",
            )
        else:
            result = await ReadFileTool(workspace=workspace).execute(
                path=str(target),
                _thread_id="thread-1",
                _thread_project_id="project-1",
            )
    finally:
        gate._current_action_permit.reset(token)

    assert "No host-access lease can open it." in result
    assert "private-material" not in result
    assert target.read_text(encoding="utf-8") == "private-material\n"
    assert consumed == []


@pytest.mark.asyncio
async def test_state_root_regression_permit_still_reads_database_file(
    tmp_path,
    monkeypatch,
):
    from infra import config
    from tools import host_access_gate
    from tools.filesystem_rw import ReadFileTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    target = state_dir / "corsia.db"
    target.write_text("diagnostic-data\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "STATE_ROOT", state_dir)
    _configure_sandbox(monkeypatch, workspace)

    async def reviewed(**_kwargs):
        return SimpleNamespace(resolved_path=target.resolve(strict=False))

    monkeypatch.setattr(host_access_gate, "consume_current_action_permit", reviewed)
    result = await ReadFileTool(workspace=workspace).execute(
        path=str(target),
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )

    assert "diagnostic-data" in result


@pytest.mark.asyncio
async def test_key_store_folder_is_refused_without_a_lease_offer(
    tmp_path,
    monkeypatch,
):
    from infra import config
    from tools import host_access_gate
    from tools.filesystem_rw import ReadFileTool

    workspace = tmp_path / "workspace"
    state_dir = tmp_path / "state"
    (state_dir / "keys").mkdir(parents=True)
    target = state_dir / "keys" / "values.json"
    target.write_text("stored-key-material\n", encoding="utf-8")
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "STATE_ROOT", state_dir)
    _configure_sandbox(monkeypatch, workspace)

    async def no_permit(**_kwargs):
        return None

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        no_permit,
    )
    result = await ReadFileTool(workspace=workspace).execute(
        path=str(target),
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )

    assert "No host-access lease can open it." in result
    assert "stored-key-material" not in result


@pytest.mark.asyncio
async def test_state_root_lexical_symlink_cannot_bypass_ordinary_review(
    tmp_path,
    monkeypatch,
):
    from infra import config
    from tools import host_access_gate
    from tools.filesystem_rw import ReadFileTool

    workspace = tmp_path / "workspace"
    state_dir = tmp_path / "state"
    public_dir = workspace / "public"
    state_dir.mkdir(parents=True)
    public_dir.mkdir(parents=True)
    (public_dir / "notes.txt").write_text("ordinary-workspace-data\n", encoding="utf-8")
    state_link = state_dir / "public-link"
    composed_alias = workspace / "composed-alias"
    state_alias = workspace / "state-alias"
    state_link.symlink_to(public_dir, target_is_directory=True)
    composed_alias.symlink_to(state_link, target_is_directory=True)
    state_alias.symlink_to(state_dir, target_is_directory=True)
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "STATE_ROOT", state_dir)
    _configure_sandbox(monkeypatch, workspace)

    async def no_permit(**_kwargs):
        return None

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        no_permit,
    )
    result = await ReadFileTool(workspace=workspace).execute(
        path=str(state_link / "notes.txt"),
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )

    assert "requires reviewed host access" in result

    composed = await ReadFileTool(workspace=workspace).execute(
        path=str(composed_alias / "notes.txt"),
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )
    assert "requires reviewed host access" in composed

    dotdot = await ReadFileTool(workspace=workspace).execute(
        path=str(state_alias / ".." / "public" / "notes.txt"),
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )
    assert "requires reviewed host access" in dotdot


@pytest.mark.asyncio
async def test_outside_write_consumes_before_first_filesystem_effect(
    tmp_path,
    monkeypatch,
):
    from tools import host_access_gate
    from tools.filesystem_rw import WriteFileTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside" / "created.txt"
    _configure_sandbox(monkeypatch, workspace)

    async def consume(**kwargs):
        assert not outside.parent.exists()
        assert kwargs["params"] == {
            "path": str(outside),
            "content": "created\n",
            "description": "test output",
            "owner": None,
            "update_trigger": None,
        }
        return SimpleNamespace(resolved_path=outside.resolve(strict=False))

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )

    result = await WriteFileTool(workspace=workspace).execute(
        path=str(outside),
        content="created\n",
        description="test output",
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )

    assert result.startswith("Successfully wrote")
    assert outside.read_text(encoding="utf-8") == "created\n"


@pytest.mark.asyncio
async def test_reviewed_private_key_read_reaches_effect_and_model_output_is_sanitized(
    tmp_path,
    monkeypatch,
):
    from tools import host_access_gate
    from tools.filesystem_rw import ReadFileTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _configure_sandbox(monkeypatch, workspace)
    home = tmp_path / "home"
    sensitive = home / ".ssh" / "id_ed25519"
    sensitive.parent.mkdir(parents=True)
    sensitive.write_text(
        "-----BEGIN PRIVATE KEY-----\n"
        "raw-private-material\n"
        "-----END PRIVATE KEY-----\n", encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    async def consume(**_kwargs):
        return SimpleNamespace(resolved_path=sensitive)

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )

    result = await ReadFileTool(workspace=workspace).execute(
        path=str(tmp_path / "requested.txt"),
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )

    assert "BEGIN PRIVATE KEY" in result
    assert "raw-private-material" in result

    from tools.registry import ToolRegistry

    registry = ToolRegistry()

    async def unredacted(_name, _params, *, review_request_id=None):
        return result

    monkeypatch.setattr(registry, "_execute_unredacted", unredacted)
    model_visible = await registry.execute("read_file", {})
    assert "raw-private-material" not in model_visible
    assert "[REDACTED]" in model_visible


@pytest.mark.asyncio
async def test_reviewed_search_does_not_apply_obsolete_descendant_veto(
    tmp_path,
    monkeypatch,
):
    from tools import host_access_gate
    from tools.grep import GrepTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    ssh = home / ".ssh"
    ssh.mkdir(parents=True)
    (ssh / "id_ed25519").write_text("TOKEN private", encoding="utf-8")
    (home / "notes.txt").write_text("TOKEN safe", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    _configure_sandbox(monkeypatch, workspace)

    async def consume(**_kwargs):
        return SimpleNamespace(resolved_path=home.resolve(strict=False))

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )

    result = await GrepTool(workspace=workspace).execute(
        pattern="TOKEN",
        path=str(tmp_path / "requested"),
        output_mode="files",
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )

    assert result == ".ssh/id_ed25519\nnotes.txt"


@pytest.mark.asyncio
async def test_every_filesystem_and_search_adapter_recomputes_exact_action(
    tmp_path,
    monkeypatch,
):
    from tools import host_access_gate
    from tools import grep as grep_module
    from tools.filesystem_edit import EditFileTool, ListDirTool, MultiEditTool
    from tools.filesystem_rw import ReadFileTool, WriteFileTool
    from tools.glob import GlobTool
    from tools.grep import GrepTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "notes.txt"
    target.write_text("alpha NEEDLE\n", encoding="utf-8")
    _configure_sandbox(monkeypatch, workspace)
    calls: list[dict] = []

    async def consume(**kwargs):
        calls.append(kwargs)
        raw_path = kwargs["params"]["path"]
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = kwargs["tool"]._workspace / path
        return SimpleNamespace(resolved_path=path.resolve(strict=False))

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(grep_module, "_RG_AVAILABLE", False)
    context = {
        "_thread_id": "thread-1",
        "_thread_project_id": "project-1",
    }

    read = await ReadFileTool(workspace=workspace).execute(
        path=str(target),
        offset=1,
        limit=2,
        **context,
    )
    edited = await EditFileTool(workspace=workspace).execute(
        path=str(target),
        old_string="alpha",
        new_string="beta",
        replace_all=False,
        **context,
    )
    multi = await MultiEditTool(workspace=workspace).execute(
        path=str(target),
        edits=[
            {
                "old_string": "beta",
                "new_string": "gamma",
                "replace_all": False,
            }
        ],
        **context,
    )
    listed = await ListDirTool(workspace=workspace).execute(
        path=str(outside),
        **context,
    )
    globbed = await GlobTool(workspace=workspace).execute(
        pattern="**/*.txt",
        path=str(outside),
        all_threads=False,
        **context,
    )
    grepped = await GrepTool(workspace=workspace).execute(
        pattern="NEEDLE",
        path=str(outside),
        glob="*.txt",
        output_mode="files",
        case_insensitive=False,
        context=0,
        max_results=7,
        all_threads=False,
        **context,
    )
    written = await WriteFileTool(workspace=workspace).execute(
        path=str(outside / "new.txt"),
        content="new\n",
        description=None,
        **context,
    )

    assert "alpha NEEDLE" in read
    assert edited.startswith("Successfully edited")
    assert multi.startswith("Successfully applied")
    assert listed == "f notes.txt"
    assert globbed == "notes.txt"
    assert grepped == "notes.txt"
    assert written.startswith("Successfully wrote")
    assert [call["tool_name"] for call in calls] == [
        "read_file",
        "edit_file",
        "multi_edit",
        "list_dir",
        "glob",
        "grep",
        "write_file",
    ]
    assert calls[0]["params"] == {
        "path": str(target),
        "offset": 1,
        "limit": 2,
    }
    assert calls[1]["params"] == {
        "path": str(target),
        "old_string": "alpha",
        "new_string": "beta",
        "replace_all": False,
    }
    assert calls[2]["params"] == {
        "path": str(target),
        "edits": [
            {
                "old_string": "beta",
                "new_string": "gamma",
                "replace_all": False,
            }
        ],
    }
    assert calls[4]["params"] == {
        "pattern": "**/*.txt",
        "path": str(outside),
        "all_threads": False,
    }
    assert calls[5]["params"] == {
        "pattern": "NEEDLE",
        "path": str(outside),
        "glob": "*.txt",
        "output_mode": "files",
        "case_insensitive": False,
        "context": 0,
        "max_results": 7,
        "all_threads": False,
    }


# Forces `_RG_AVAILABLE = True` and then asserts REAL ripgrep output (context
# lines, brace-glob expansion). Telling the code the binary exists does not put
# it there: without `rg` on PATH the tool returns
# "Error: [Errno 2] No such file or directory: 'rg'", so this could only fail —
# it was red on every machine without ripgrep, including CI. Requiring the
# binary is the honest gate, because every assertion here is about ripgrep's own
# semantics and there is nothing to verify in its absence.
@pytest.mark.skipif(
    shutil.which("rg") is None,
    reason="asserts real ripgrep semantics; requires the rg binary on PATH",
)
@pytest.mark.asyncio
async def test_reviewed_grep_preserves_context_and_brace_glob_semantics(
    tmp_path,
    monkeypatch,
):
    from tools import grep as grep_module
    from tools import host_access_gate
    from tools.grep import GrepTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.ts").write_text(
        "before\nNEEDLE\nafter\n",
        encoding="utf-8",
    )
    (workspace / "skip.py").write_text(
        "before\nNEEDLE\nafter\n",
        encoding="utf-8",
    )
    _configure_sandbox(monkeypatch, workspace)
    reviewed = False

    async def consume(**_kwargs):
        if reviewed:
            return SimpleNamespace(resolved_path=workspace.resolve())
        return None

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(grep_module, "_RG_AVAILABLE", True)
    tool = GrepTool(workspace=workspace)
    params = {
        "pattern": "NEEDLE",
        "path": str(workspace),
        "glob": "*.{ts,tsx}",
        "output_mode": "content",
        "context": 1,
        "max_results": 50,
        "_thread_id": "thread-1",
        "_thread_project_id": "project-1",
    }

    ordinary_result = await tool.execute(**params)
    reviewed = True
    reviewed_result = await tool.execute(**params)

    assert reviewed_result == ordinary_result
    assert "main.ts-1-before" in reviewed_result
    assert "main.ts:2:NEEDLE" in reviewed_result
    assert "main.ts-3-after" in reviewed_result
    assert "skip.py" not in reviewed_result


@pytest.mark.asyncio
@pytest.mark.parametrize("search_kind", ["file", "directory"])
async def test_reviewed_grep_skips_oversized_files_before_read(
    search_kind,
    tmp_path,
    monkeypatch,
):
    from tools import filesystem_effects, host_access_gate
    from tools import grep as grep_module
    from tools.grep import GrepTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    oversized = outside / "oversized.txt"
    with oversized.open("wb") as handle:
        handle.truncate(10 * 1024 * 1024 + 1)
    _configure_sandbox(monkeypatch, workspace)
    search_path = oversized if search_kind == "file" else outside

    async def consume(**_kwargs):
        return SimpleNamespace(resolved_path=search_path.resolve())

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )

    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("oversized reviewed file must not be read")

    if search_kind == "file":
        @contextmanager
        def oversized_file(_path):
            yield (
                SimpleNamespace(read=unexpected_read),
                SimpleNamespace(st_size=10 * 1024 * 1024 + 1),
            )

        monkeypatch.setattr(grep_module, "open_bound_text", oversized_file)
    else:
        monkeypatch.setattr(
            filesystem_effects.BoundTreeFile,
            "read_text",
            unexpected_read,
        )

    result = await GrepTool(workspace=workspace).execute(
        pattern="NEEDLE",
        path=str(search_path),
        output_mode="files",
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )

    assert result == "No matches for pattern 'NEEDLE'"


@pytest.mark.asyncio
async def test_reviewed_protected_repo_write_executes_but_no_permit_stays_blocked(
    tmp_path,
    monkeypatch,
):
    from infra.project_repo_policy import RepoPolicy
    from tools import host_access_gate
    from tools.filesystem_rw import WriteFileTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    target = repo / "tracked.txt"
    target.write_text("original\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "tracked.txt"],
        check=True,
        capture_output=True,
        text=True,
    )
    _configure_sandbox(
        monkeypatch,
        workspace,
        [RepoPolicy("project-1", "repo", repo.resolve(), None, True)],
    )

    consumed = []

    async def reviewed(**kwargs):
        consumed.append(kwargs)
        return SimpleNamespace(resolved_path=target.resolve(strict=False))

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        reviewed,
    )

    result = await WriteFileTool(workspace=workspace).execute(
        path=str(target),
        content="changed\n",
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )

    assert result.startswith("Successfully wrote")
    assert target.read_text(encoding="utf-8") == "changed\n"
    assert len(consumed) == 1

    target.write_text("reset\n", encoding="utf-8")

    async def no_permit(**_kwargs):
        return None

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        no_permit,
    )
    protected = await WriteFileTool(workspace=workspace).execute(
        path=str(target),
        content="blocked\n",
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )

    assert "protected base checkout" in protected
    assert target.read_text(encoding="utf-8") == "reset\n"


@pytest.mark.asyncio
async def test_symlink_target_drift_blocks_before_read_effect(
    tmp_path,
    monkeypatch,
):
    from tools import host_access_gate
    from tools.filesystem_rw import ReadFileTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "secret.txt").write_text("first\n", encoding="utf-8")
    (second / "secret.txt").write_text("second\n", encoding="utf-8")
    alias = tmp_path / "alias"
    alias.symlink_to(first, target_is_directory=True)
    requested = alias / "secret.txt"
    reviewed_target = requested.resolve(strict=False)
    alias.unlink()
    alias.symlink_to(second, target_is_directory=True)
    _configure_sandbox(monkeypatch, workspace)

    async def consume(**kwargs):
        assert kwargs["params"]["path"] == str(requested)
        assert requested.resolve(strict=False) != reviewed_target
        raise host_access_gate.HostAccessBlocked(
            "Host action changed before execution."
        )

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )

    result = await ReadFileTool(workspace=workspace).execute(
        path=str(requested),
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )

    assert result == "Error: Host action changed before execution."
    assert "first" not in result
    assert "second" not in result


@pytest.mark.asyncio
async def test_post_consume_ancestor_swap_cannot_redirect_read(
    tmp_path,
    monkeypatch,
):
    from tools import host_access_gate
    from tools.filesystem_rw import ReadFileTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    reviewed_root = tmp_path / "reviewed"
    reviewed_root.mkdir()
    reviewed_target = reviewed_root / "secret.txt"
    reviewed_target.write_text("reviewed\n", encoding="utf-8")
    redirected_root = tmp_path / "redirected"
    redirected_root.mkdir()
    (redirected_root / "secret.txt").write_text("redirected\n", encoding="utf-8")
    displaced_root = tmp_path / "displaced"
    _configure_sandbox(monkeypatch, workspace)

    async def consume(**_kwargs):
        reviewed_root.rename(displaced_root)
        reviewed_root.symlink_to(redirected_root, target_is_directory=True)
        return SimpleNamespace(resolved_path=reviewed_target)

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )

    result = await ReadFileTool(workspace=workspace).execute(
        path=str(reviewed_target),
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )

    assert result.startswith("Error")
    assert "\treviewed" not in result
    assert "\tredirected" not in result


@pytest.mark.asyncio
async def test_post_consume_ancestor_swap_cannot_redirect_write(
    tmp_path,
    monkeypatch,
):
    from tools import host_access_gate
    from tools.filesystem_rw import WriteFileTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    reviewed_root = tmp_path / "reviewed"
    reviewed_root.mkdir()
    reviewed_target = reviewed_root / "notes.txt"
    reviewed_target.write_text("reviewed\n", encoding="utf-8")
    redirected_root = tmp_path / "redirected"
    redirected_root.mkdir()
    redirected_target = redirected_root / "notes.txt"
    redirected_target.write_text("must-stay\n", encoding="utf-8")
    displaced_root = tmp_path / "displaced"
    _configure_sandbox(monkeypatch, workspace)

    async def consume(**_kwargs):
        reviewed_root.rename(displaced_root)
        reviewed_root.symlink_to(redirected_root, target_is_directory=True)
        return SimpleNamespace(resolved_path=reviewed_target)

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )

    result = await WriteFileTool(workspace=workspace).execute(
        path=str(reviewed_target),
        content="changed\n",
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )

    assert result.startswith("Error")
    assert redirected_target.read_text(encoding="utf-8") == "must-stay\n"
    assert (displaced_root / "notes.txt").read_text(encoding="utf-8") == "reviewed\n"


@pytest.mark.asyncio
async def test_swap_after_parent_binding_reads_original_authorized_object(
    tmp_path,
    monkeypatch,
):
    from tools import filesystem_effects, host_access_gate
    from tools.filesystem_rw import ReadFileTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    reviewed_root = tmp_path / "reviewed"
    reviewed_root.mkdir()
    reviewed_target = reviewed_root / "secret.txt"
    reviewed_target.write_text("reviewed\n", encoding="utf-8")
    redirected_root = tmp_path / "redirected"
    redirected_root.mkdir()
    (redirected_root / "secret.txt").write_text("redirected\n", encoding="utf-8")
    displaced_root = tmp_path / "displaced"
    _configure_sandbox(monkeypatch, workspace)

    async def consume(**_kwargs):
        return SimpleNamespace(resolved_path=reviewed_target)

    original_open = filesystem_effects.BoundParent.open_file
    swapped = False

    def swap_then_open(parent, flags, *, mode=0o666):
        nonlocal swapped
        if not swapped:
            swapped = True
            reviewed_root.rename(displaced_root)
            reviewed_root.symlink_to(redirected_root, target_is_directory=True)
        return original_open(parent, flags, mode=mode)

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(
        filesystem_effects.BoundParent,
        "open_file",
        swap_then_open,
    )

    result = await ReadFileTool(workspace=workspace).execute(
        path=str(reviewed_target),
        _thread_id="thread-1",
        _thread_project_id="project-1",
    )

    assert "\treviewed" in result
    assert "\tredirected" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["glob", "grep"])
async def test_post_consume_ancestor_swap_cannot_redirect_search(
    tool_name,
    tmp_path,
    monkeypatch,
):
    from tools import grep as grep_module
    from tools import host_access_gate
    from tools.glob import GlobTool
    from tools.grep import GrepTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    reviewed_root = tmp_path / "reviewed"
    reviewed_root.mkdir()
    (reviewed_root / "reviewed.txt").write_text(
        "REVIEWED_NEEDLE\n",
        encoding="utf-8",
    )
    redirected_root = tmp_path / "redirected"
    redirected_root.mkdir()
    (redirected_root / "redirected.txt").write_text(
        "REDIRECTED_NEEDLE\n",
        encoding="utf-8",
    )
    displaced_root = tmp_path / "displaced"
    _configure_sandbox(monkeypatch, workspace)

    async def consume(**_kwargs):
        reviewed_root.rename(displaced_root)
        reviewed_root.symlink_to(redirected_root, target_is_directory=True)
        return SimpleNamespace(resolved_path=reviewed_root)

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(grep_module, "_RG_AVAILABLE", False)
    context = {
        "_thread_id": "thread-1",
        "_thread_project_id": "project-1",
    }

    if tool_name == "glob":
        result = await GlobTool(workspace=workspace).execute(
            pattern="**/*.txt",
            path=str(reviewed_root),
            **context,
        )
    else:
        result = await GrepTool(workspace=workspace).execute(
            pattern="NEEDLE",
            path=str(reviewed_root),
            output_mode="files",
            **context,
        )

    assert result.startswith("Error")
    assert "redirected.txt" not in result
    assert "REDIRECTED_NEEDLE" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["edit_file", "multi_edit", "list_dir"])
async def test_post_consume_ancestor_swap_cannot_redirect_edit_or_list(
    tool_name,
    tmp_path,
    monkeypatch,
):
    from tools import host_access_gate
    from tools.filesystem_edit import EditFileTool, ListDirTool, MultiEditTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    reviewed_root = tmp_path / "reviewed"
    reviewed_root.mkdir()
    reviewed_target = reviewed_root / "notes.txt"
    reviewed_target.write_text("reviewed alpha\n", encoding="utf-8")
    redirected_root = tmp_path / "redirected"
    redirected_root.mkdir()
    redirected_target = redirected_root / "notes.txt"
    redirected_target.write_text("redirected alpha\n", encoding="utf-8")
    displaced_root = tmp_path / "displaced"
    _configure_sandbox(monkeypatch, workspace)

    async def consume(**_kwargs):
        reviewed_root.rename(displaced_root)
        reviewed_root.symlink_to(redirected_root, target_is_directory=True)
        resolved = reviewed_root if tool_name == "list_dir" else reviewed_target
        return SimpleNamespace(resolved_path=resolved)

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    context = {
        "_thread_id": "thread-1",
        "_thread_project_id": "project-1",
    }

    if tool_name == "edit_file":
        result = await EditFileTool(workspace=workspace).execute(
            path=str(reviewed_target),
            old_string="alpha",
            new_string="beta",
            **context,
        )
    elif tool_name == "multi_edit":
        result = await MultiEditTool(workspace=workspace).execute(
            path=str(reviewed_target),
            edits=[{"old_string": "alpha", "new_string": "beta"}],
            **context,
        )
    else:
        result = await ListDirTool(workspace=workspace).execute(
            path=str(reviewed_root),
            **context,
        )

    assert result.startswith("Error")
    assert redirected_target.read_text(encoding="utf-8") == "redirected alpha\n"
    assert (displaced_root / "notes.txt").read_text(
        encoding="utf-8"
    ) == "reviewed alpha\n"


@pytest.mark.asyncio
async def test_recursive_search_never_reuses_root_authority_for_symlink_escape(
    tmp_path,
    monkeypatch,
):
    from tools import host_access_gate
    from tools import grep as grep_module
    from tools.glob import GlobTool
    from tools.grep import GrepTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    search_root = tmp_path / "outside-root"
    search_root.mkdir()
    outside = tmp_path / "outside-secret.md"
    outside.write_text("ESCAPE_NEEDLE\n", encoding="utf-8")
    alias = search_root / "outside-link.md"
    alias.symlink_to(outside)
    _configure_sandbox(monkeypatch, workspace)
    calls = []

    async def consume(**kwargs):
        calls.append(kwargs["tool_name"])
        return SimpleNamespace(
            resolved_path=search_root.resolve(strict=False)
        )

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        consume,
    )
    monkeypatch.setattr(grep_module, "_RG_AVAILABLE", False)
    context = {
        "_thread_id": "thread-1",
        "_thread_project_id": "project-1",
    }

    grep = await GrepTool(workspace=workspace).execute(
        pattern="ESCAPE_NEEDLE",
        path=str(search_root),
        output_mode="files",
        **context,
    )
    glob = await GlobTool(workspace=workspace).execute(
        pattern="**/*.md",
        path=str(search_root),
        **context,
    )

    assert grep == "No matches for pattern 'ESCAPE_NEEDLE'"
    assert glob == f"No files matching '**/*.md' in {search_root.resolve()}"
    assert calls == ["grep", "glob"]


@pytest.mark.asyncio
@pytest.mark.parametrize("from_parent", [False, True])
async def test_reviewed_search_never_reads_or_lists_the_key_store(
    from_parent,
    tmp_path,
    monkeypatch,
):
    from infra import config
    from tools import host_access_gate
    from tools import grep as grep_module
    from tools.glob import GlobTool
    from tools.grep import GrepTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    (state_dir / "keys").mkdir(parents=True)
    (state_dir / "keys" / "values.json").write_text("NEEDLE key-material\n", encoding="utf-8")
    (state_dir / "notes.json").write_text("NEEDLE sibling-state\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "STATE_ROOT", state_dir)
    monkeypatch.setattr(grep_module, "_RG_AVAILABLE", False)
    _configure_sandbox(monkeypatch, workspace)
    search_root = tmp_path if from_parent else state_dir

    async def consume(**_kwargs):
        return SimpleNamespace(resolved_path=search_root.resolve(strict=False))

    monkeypatch.setattr(host_access_gate, "consume_current_action_permit", consume)
    context = {"_thread_id": "thread-1", "_thread_project_id": "project-1"}

    grepped = await GrepTool(workspace=workspace).execute(
        pattern="NEEDLE", path=str(search_root), output_mode="content", **context,
    )
    globbed = await GlobTool(workspace=workspace).execute(
        pattern="**/*.json", path=str(search_root), **context,
    )

    assert "sibling-state" in grepped
    assert "key-material" not in grepped
    assert "notes.json" in globbed
    assert "values.json" not in globbed
