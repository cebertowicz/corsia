"""A confined (``review``) native or local worker reads only its read paths.

Every secret stand-in sits where the ordinary boundary would let an unconfined
thread read it, so each refusal here is the confinement's, not the sandbox's.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import tools.confined_reads as confined_reads


def _configure_sandbox(monkeypatch, workspace: Path) -> None:
    from tools import sandbox_config

    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [workspace])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(sandbox_config, "_list_authoritative_repo_policies", lambda **_filters: [])


@pytest.fixture
def tree(tmp_path, monkeypatch):
    from infra import config
    from tools import host_access_gate

    workspace = tmp_path / "workspace"
    grant = workspace / "grant"
    grant.mkdir(parents=True)
    (grant / "notes.md").write_text("granted body\n", encoding="utf-8")
    ssh = workspace / "home" / ".ssh"
    ssh.mkdir(parents=True)
    (ssh / "id_ed25519").write_text("SSH SECRET\n", encoding="utf-8")
    state = tmp_path / "state"
    (state / "keys").mkdir(parents=True)
    (state / "keys" / "values.json").write_text("KEY STORE SECRET\n", encoding="utf-8")
    (grant / "link.md").symlink_to(ssh / "id_ed25519")
    (grant / "linkdir").symlink_to(ssh, target_is_directory=True)
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "STATE_ROOT", state)
    _configure_sandbox(monkeypatch, workspace)
    permits = []

    async def reviewed(**kwargs):
        # A reviewed permit for any path: only the confinement can stop the read.
        permits.append(kwargs)
        return SimpleNamespace(resolved_path=Path(kwargs["params"]["path"]).resolve())

    monkeypatch.setattr(host_access_gate, "consume_current_action_permit", reviewed)
    return SimpleNamespace(workspace=workspace, grant=grant, ssh=ssh, state=state, permits=permits)


def _review(grant: Path) -> dict:
    return {"confinement": "review", "confined_read_dirs": json.dumps([str(grant)])}


async def _read(tree, path: str) -> str:
    from tools.filesystem_rw import ReadFileTool

    return str(await ReadFileTool(workspace=tree.workspace).execute(
        path=path, _thread_id="worker", _thread_project_id=None,
    ))


@pytest.mark.asyncio
async def test_a_confined_read_reaches_its_grant_and_nothing_else(tree) -> None:
    with confined_reads.scope(_review(tree.grant)):
        assert "granted body" in await _read(tree, str(tree.grant / "notes.md"))
        for path in (
            str(tree.ssh / "id_ed25519"),                       # an ~/.ssh stand-in
            str(tree.state / "keys" / "values.json"),           # the key store stand-in
            str(tree.grant / "link.md"),                        # a symlink out of the grant
            str(tree.grant / ".." / "home" / ".ssh" / "id_ed25519"),  # a .. escape
            "home/.ssh/id_ed25519",                             # relative to the workspace
        ):
            result = await _read(tree, path)
            assert "SECRET" not in result
            assert "outside this confined worker's read paths" in result, path
    assert tree.permits == []  # no reviewed host permit was ever taken


@pytest.mark.asyncio
async def test_an_unconfined_read_is_unchanged(tree) -> None:
    assert "SSH SECRET" in await _read(tree, str(tree.grant / "link.md"))


@pytest.mark.asyncio
async def test_a_confined_worker_cannot_write_even_inside_its_grant(tree) -> None:
    from tools.filesystem_rw import WriteFileTool

    with confined_reads.scope(_review(tree.grant)):
        result = await WriteFileTool(workspace=tree.workspace).execute(
            path=str(tree.grant / "new.md"), content="x", _thread_id="worker",
            _thread_project_id=None,
        )
    assert "cannot write" in str(result)
    assert not (tree.grant / "new.md").exists()


@pytest.mark.asyncio
async def test_confined_grep_and_glob_stay_in_the_grant(tree) -> None:
    from tools.glob import GlobTool
    from tools.grep import GrepTool

    grep = GrepTool(workspace=tree.workspace)
    glob = GlobTool(workspace=tree.workspace)
    with confined_reads.scope(_review(tree.grant)):
        outside = await grep.execute(pattern="SECRET", path=str(tree.workspace), _thread_id="worker")
        inside = await grep.execute(pattern="SECRET|granted", path=str(tree.grant), output_mode="content",
                                    _thread_id="worker")
        listed = await glob.execute(pattern="**/*", path=str(tree.grant), _thread_id="worker")
        linked = await glob.execute(pattern="linkdir/*", path=str(tree.grant), _thread_id="worker")
    assert "outside this confined worker's read paths" in str(outside)
    assert "granted body" in str(inside) and "SECRET" not in str(inside)
    assert "link.md" not in str(inside)
    assert "notes.md" in str(listed) and "id_ed25519" not in str(listed)
    assert "id_ed25519" not in str(linked)


@pytest.mark.asyncio
async def test_default_search_finds_worker_inputs_but_not_another_thread(tree) -> None:
    from tools.glob import GlobTool
    from tools.grep import GrepTool

    inputs = tree.workspace / ".threads" / "owner" / "workers" / "1"
    other = tree.workspace / ".threads" / "other"
    inputs.mkdir(parents=True)
    other.mkdir(parents=True)
    (inputs / "brief.md").write_text("worker input needle", encoding="utf-8")
    (other / "secret.md").write_text("other needle", encoding="utf-8")
    with confined_reads.scope(_review(inputs)):
        grep = GrepTool(workspace=tree.workspace)
        glob = GlobTool(workspace=tree.workspace)
        found = await grep.execute(pattern="needle", path=str(inputs), _thread_id="worker")
        listed = await glob.execute(pattern="**/*.md", path=str(inputs), _thread_id="worker")
        refused = await grep.execute(pattern="needle", path=str(other), _thread_id="worker")
    assert "brief.md" in found and "secret.md" not in found
    assert "brief.md" in listed and "secret.md" not in listed
    assert "outside this confined worker's read paths" in refused
    with confined_reads.scope(_review(tree.workspace)):
        broad = await glob.execute(pattern="**/*.md", path=str(tree.workspace),
                                   all_threads=True, _thread_id="worker")
    assert "secret.md" not in broad and "brief.md" not in broad


def test_only_the_read_tools_pass_the_boundary() -> None:
    review = _review(Path("/tmp/grant"))
    for name in confined_reads.REVIEW_READ_TOOLS:
        assert confined_reads.call_refusal(name, review) is None
    for name in ("bash", "write_file", "edit_file", "web_fetch", "delegate_task", "tool_call",
                 "tools_load", "plan_update", "read_tool_result"):
        assert "not available to a confined review worker" in confined_reads.call_refusal(name, review)
    assert confined_reads.call_refusal("bash", {"confinement": None}) is None
    damaged = {"confinement": "review", "confined_read_dirs": "[]"}
    assert "no tool may run" in confined_reads.call_refusal("read_file", damaged)


def _seed_thread(db_path: Path, thread_id: str, grant: Path | None) -> None:
    from infra import thread_confinement

    with sqlite3.connect(db_path) as db:
        db.execute("INSERT OR IGNORE INTO users (id,password_hash,created_at,password_changed_at) "
                   "VALUES ('u','x','t','t')")
        db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) "
                   "VALUES (?,'u','t','t','t')", (thread_id,))
        if grant is not None:
            thread_confinement.record(db, thread_id, "review", (str(grant),))


@pytest.mark.asyncio
async def test_the_registry_refuses_a_confined_threads_other_tools(isolated_db, tree) -> None:
    from infra import state
    from tools.filesystem_rw import ReadFileTool
    from tools.registry import ToolRegistry
    from tools.shell import BashTool

    _seed_thread(isolated_db, "worker", tree.grant)
    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace=tree.workspace))
    registry.register(BashTool(working_dir=str(tree.workspace)))
    marker = state._current_thread_id.set("worker")
    try:
        shell = await registry.execute("bash", {"command": "cat home/.ssh/id_ed25519"})
        dispatched = await registry.execute(
            "tool_call", {"name": "bash", "arguments": {"command": "cat home/.ssh/id_ed25519"}},
        )
        secret = await registry.execute("read_file", {"path": str(tree.ssh / "id_ed25519")})
        granted = await registry.execute("read_file", {"path": str(tree.grant / "notes.md")})
    finally:
        state._current_thread_id.reset(marker)
    for refused in (shell, dispatched):
        assert "not available to a confined review worker" in refused and "SECRET" not in refused
    assert "outside this confined worker's read paths" in secret and "SECRET" not in secret
    assert "granted body" in granted


@pytest.mark.asyncio
async def test_a_confined_thread_is_offered_only_the_read_tools(isolated_db, tree, monkeypatch) -> None:
    from tools.filesystem_rw import ReadFileTool, WriteFileTool
    from tools.registry import ToolRegistry
    from tools.shell import BashTool

    _seed_thread(isolated_db, "worker", tree.grant)
    _seed_thread(isolated_db, "ordinary", None)
    registry = ToolRegistry()
    for tool in (ReadFileTool(workspace=tree.workspace), WriteFileTool(workspace=tree.workspace),
                 BashTool(working_dir=str(tree.workspace))):
        registry.register(tool)

    def names(definitions) -> set[str]:
        return {d["function"]["name"] for d in definitions}

    with sqlite3.connect(isolated_db) as db:
        db.row_factory = sqlite3.Row
        rows = {row['id']: dict(row) for row in db.execute(
            "SELECT id, confinement, confined_read_dirs FROM threads WHERE id IN ('worker', 'ordinary')")}
    def no_db():
        raise AssertionError('advertising must reuse the loaded thread row')
    monkeypatch.setattr('infra.database.get_db', no_db)
    assert names(await confined_reads.advertised(registry, rows['worker'])) == {"read_file"}
    assert {"read_file", "write_file", "bash"} <= names(await confined_reads.advertised(registry, rows['ordinary']))
    rows['worker']['confined_read_dirs'] = 'broken'
    assert await confined_reads.advertised(registry, rows['worker']) == ()
