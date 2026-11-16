"""A registration inside its own project's folder waits only for work that can reach that project.

Driven through the real ToolRegistry (effect admission and reach), the real
ProcessOwner (launch reach on the record), the real add procedure and the real
project fence, on a real schema database. Only process-wide seams move: the
workspace root, home, the host-action gate's decision, and the DB thread.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tools.base import Tool

_PERMIT = object()


def _git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git = ["git", "-C", str(path), "-c", "user.name=T", "-c", "user.email=t@corsia.local"]
    subprocess.run(["git", "-c", "init.defaultRefFormat=files", "init", "-b", "main", str(path)],
                   check=True, capture_output=True)
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run([*git, "add", "tracked.txt"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-m", "base"], check=True, capture_output=True)


class _HoldTool(Tool):
    """Holds its call open until released; optionally launches a background owned process first."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.handles: list[str] = []

    @property
    def name(self) -> str:
        return "hold"

    @property
    def description(self) -> str:
        return "Hold a call open."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"spawn": {"type": "boolean"}},
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        from host_access import processes

        if kwargs.get("spawn"):
            started = await processes.PROCESS_OWNER.spawn(
                [sys.executable, "-c", "import time;time.sleep(60)"],
                thread_id=kwargs["_thread_id"], turn_id="turn-1", cwd=Path.cwd(),
                env=os.environ, timeout_seconds=60.0, background=True, description="sleeper",
            )
            self.handles.append(started.handle)
            return "spawned"
        self.entered.set()
        await self.release.wait()
        return "released"


async def _run_inline(fn, *args, **kwargs):
    return fn(*args, **kwargs)


async def _record_transition(_record, _kind) -> None:
    """The durable background callback boundary, acknowledged."""


@pytest.fixture
async def env(isolated_db, tmp_path, monkeypatch):
    from api import project_repos
    from host_access import processes
    from infra.database import get_db
    from tools import host_access_gate
    from tools.registry import ToolRegistry
    from tools.project_repo_register import ProjectRepoRegisterTool

    ws = tmp_path / "ws"
    alpha = ws / "projects" / "alpha"
    beta = ws / "projects" / "beta"
    for folder in (alpha / "source", beta / "source", ws / "shared"):
        folder.mkdir(parents=True)
    monkeypatch.setattr("infra.config.WORKSPACE", ws)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(project_repos, "sync_sources", lambda *_args: None)
    monkeypatch.setattr(project_repos, "run_in_db", _run_inline)
    monkeypatch.setattr("infra.db_async.run_in_db", _run_inline, raising=False)
    monkeypatch.setattr("tools.dispatch_ownership.run_in_db", _run_inline, raising=False)
    monkeypatch.setattr("tools.project_repo_register.run_in_db", _run_inline, raising=False)

    leased: set[str] = set()

    async def authorize(**kwargs):
        if kwargs["thread_id"] in leased:
            return host_access_gate.HostActionAuthorization(_permit=_PERMIT)
        return host_access_gate.HostActionAuthorization()

    monkeypatch.setattr(host_access_gate, "authorize_host_action", authorize)
    monkeypatch.setattr(host_access_gate, "host_action_permit_scope", _no_permit_scope)

    owner = processes.ProcessOwner(runtime_dir=tmp_path / "runtime")
    owner.configure_transition_callback(_record_transition)
    monkeypatch.setattr(processes, "PROCESS_OWNER", owner)

    db = get_db()
    for pid, name, folder in (("pa", "Alpha", alpha), ("pb", "Beta", beta)):
        db.execute(
            "INSERT INTO projects (id,name,path,created_at,updated_at) VALUES (?,?,?,'t','t')",
            (pid, name, str(folder)),
        )
    for tid, project, title in (
        ("reg", "pa", "Registrar"), ("p-agent", "pa", "Alpha worker"),
        ("q-agent", "pb", "Beta worker"), ("q-agent-2", "pb", "Beta second"),
    ):
        db.execute(
            "INSERT INTO threads (id,user_id,model,project_id,title,created_at,updated_at) "
            "VALUES (?,'u-test','test/model',?,?,'t','t')",
            (tid, project, title),
        )
    db.commit()

    registry = ToolRegistry()
    hold = _HoldTool()
    registry.register(hold)
    registry.register(ProjectRepoRegisterTool())
    yield {
        "db": db, "ws": ws, "alpha": alpha, "beta": beta, "tmp": tmp_path,
        "registry": registry, "hold": hold, "owner": owner, "leased": leased,
    }
    hold.release.set()
    await owner.shutdown()
    db.close()


@contextmanager
def _no_permit_scope(_authorization):
    yield


async def _call(registry, thread_id: str, name: str, params: dict) -> str:
    from infra import state

    token = state._current_thread_id.set(thread_id)
    try:
        return await registry.execute(name, params)
    finally:
        state._current_thread_id.reset(token)


async def _held_effect(env, thread_id: str) -> asyncio.Task:
    hold = env["hold"]
    hold.entered.clear()
    task = asyncio.create_task(_call(env["registry"], thread_id, "hold", {}))
    await asyncio.wait_for(hold.entered.wait(), 10)
    return task


async def _release(env, task: asyncio.Task) -> None:
    env["hold"].release.set()
    assert await task == "released"
    env["hold"].release.clear()


async def _register(env, target: Path) -> str:
    return await _call(env["registry"], "reg", "project_repo_register", {"repo_path": str(target)})


def _registered(env) -> int:
    return env["db"].execute("SELECT COUNT(*) FROM project_repo_policies").fetchone()[0]


def _own_target(env) -> Path:
    target = env["alpha"] / "code" / "app"
    _git_repo(target)
    return target


@pytest.mark.asyncio
async def test_registers_in_own_folder_while_other_project_effect_and_process_are_live(env):
    target = _own_target(env)
    spawned = await _call(env["registry"], "q-agent", "hold", {"spawn": True})
    assert spawned == "spawned"
    assert env["owner"].active_process_count() == 1
    task = await _held_effect(env, "q-agent-2")
    try:
        result = await _register(env, target)
    finally:
        await _release(env, task)
    assert "Registered repository 'app'" in result, result
    assert _registered(env) == 1


@pytest.mark.parametrize("blocker", ["same project", "leased", "unreadable caller"])
@pytest.mark.asyncio
async def test_effect_that_can_reach_the_project_refuses_and_is_named(env, monkeypatch, blocker):
    from infra import project_fence

    target = _own_target(env)
    thread = "p-agent" if blocker == "same project" else "q-agent"
    if blocker == "leased":
        env["leased"].add("q-agent")
    if blocker == "unreadable caller":
        real = project_fence.read_caller

        def failing(thread_id, *, db):
            if thread_id == "q-agent":
                raise RuntimeError("caller unreadable")
            return real(thread_id, db=db)

        monkeypatch.setattr(project_fence, "read_caller", failing)
    task = await _held_effect(env, thread)
    try:
        result = await _register(env, target)
    finally:
        await _release(env, task)
    title = "Alpha worker" if blocker == "same project" else "Beta worker"
    assert (
        f"Repositories can't change while tool `hold` from agent \"{title}\" is running. "
        "Try again once it finishes."
    ) in result, result
    assert _registered(env) == 0


@pytest.mark.asyncio
async def test_live_connected_app_call_from_another_project_refuses(env, monkeypatch):
    """A connected app's local server is not project-fenced, so its call reaches everywhere."""
    from types import SimpleNamespace

    from tools.mcp import MCPToolWrapper

    class _HoldConnectedApp(MCPToolWrapper):
        async def execute(self, **_kwargs: Any) -> str:
            env["hold"].entered.set()
            await env["hold"].release.wait()
            return "released"

    app = _HoldConnectedApp(
        None, "files", SimpleNamespace(name="hold", description="Hold.", inputSchema=None),
        app_id="app-files",
    )
    env["registry"].register(app)
    # Beta's agent was granted this connected app; the grant itself is not under test.
    monkeypatch.setattr(
        "tools.tool_execution_context.mcp_tool_reaches_thread",
        lambda _tool, thread_id, *, db: thread_id == "q-agent",
    )
    target = _own_target(env)
    env["hold"].entered.clear()
    task = asyncio.create_task(_call(env["registry"], "q-agent", app.name, {}))
    entered = asyncio.ensure_future(env["hold"].entered.wait())
    await asyncio.wait({task, entered}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
    assert entered.done() and not task.done(), task.result() if task.done() else "never entered"
    try:
        result = await _register(env, target)
    finally:
        await _release(env, task)
    assert f"while tool `{app.name}` from agent \"Beta worker\" is running." in result, result
    assert _registered(env) == 0


@pytest.mark.asyncio
async def test_person_session_effect_refuses(env):
    from infra.repo_policy_transition import admit_repo_effect, caller_reach

    target = _own_target(env)
    with admit_repo_effect(caller_reach(None, db=env["db"]), tool_name="settings"):
        result = await _register(env, target)
    assert "while tool `settings` from the person's own session is running." in result, result
    assert _registered(env) == 0


@pytest.mark.parametrize("blocker", ["same project", "no recorded reach", "moved thread"])
@pytest.mark.asyncio
async def test_process_that_can_reach_the_project_refuses_and_is_named(env, blocker):
    target = _own_target(env)
    if blocker == "no recorded reach":
        await env["owner"].spawn(
            [sys.executable, "-c", "import time;time.sleep(60)"],
            thread_id="q-agent", turn_id="turn-1", cwd=Path.cwd(), env=os.environ,
            timeout_seconds=60.0, background=True, description="sleeper",
        )
        owner_title = "Beta worker"
    else:
        assert await _call(env["registry"], "p-agent", "hold", {"spawn": True}) == "spawned"
        owner_title = "Alpha worker"
        if blocker == "moved thread":
            # Its launch reach, not the thread's current project, still decides.
            env["db"].execute("UPDATE threads SET project_id='pb' WHERE id='p-agent'")
            env["db"].commit()
    result = await _register(env, target)
    assert f"while process \"sleeper\" from agent \"{owner_title}\" is running." in result, result
    assert _registered(env) == 0


@pytest.mark.parametrize("where", ["outside workspace", "workspace outside projects"])
@pytest.mark.asyncio
async def test_targets_outside_own_folder_keep_the_instance_wide_fence(env, where):
    target = (
        env["tmp"] / "external" / "app" if where == "outside workspace"
        else env["ws"] / "shared" / "app"
    )
    _git_repo(target)
    task = await _held_effect(env, "q-agent")
    try:
        result = await _register(env, target)
    finally:
        await _release(env, task)
    assert "Repositories can't change while tool `hold` from agent" in result, result
    assert _registered(env) == 0
    # Once nothing is in flight the same registration goes through, as today.
    assert "Registered repository 'app'" in await _register(env, target)


@pytest.mark.asyncio
async def test_target_in_another_projects_folder_keeps_the_instance_wide_fence(env):
    from fastapi import HTTPException

    from api import project_repos

    target = env["beta"] / "code" / "app"
    _git_repo(target)
    task = await _held_effect(env, "q-agent")
    try:
        with pytest.raises(HTTPException) as raised:
            project_repos.register_source_repo(
                "pa", project_repos.RepoAdd(mode="link", path=str(target), name="app"),
            )
    finally:
        await _release(env, task)
    assert raised.value.status_code == 409
    assert "Repositories can't change while tool `hold`" in raised.value.detail
    assert _registered(env) == 0


@pytest.mark.asyncio
async def test_other_project_cannot_write_the_registered_folder_or_repo(env):
    from tools.project_fence import fence_for_thread

    target = _own_target(env)
    assert "Registered repository 'app'" in await _register(env, target)
    fence = fence_for_thread("q-agent", db=env["db"])
    for path in (target / "tracked.txt", env["alpha"] / "source" / "app" / "new.txt", env["alpha"] / "notes.md"):
        assert fence.refusal(path) is not None, path
    assert fence_for_thread("p-agent", db=env["db"]).refusal(target / "tracked.txt") is None


@pytest.mark.asyncio
async def test_effect_arriving_during_a_narrowed_registration_is_refused_only_if_it_reaches(env):
    from infra.repo_policy_transition import (
        EVERYWHERE, Reach, RepoPolicyTransitionBusy, admit_repo_effect, repo_policy_transition,
        transition_generation,
    )

    with repo_policy_transition(
        active_process_count=lambda: 0, owned_processes=lambda: (),
        claim=frozenset({"pa"}), claim_generation=transition_generation(),
    ):
        with admit_repo_effect(Reach(frozenset({"pa"}))):
            pass
        for reach in (Reach(frozenset({"pb"})), EVERYWHERE):
            with pytest.raises(RepoPolicyTransitionBusy, match="active policy transition"):
                with admit_repo_effect(reach):
                    pass


def test_claim_is_own_project_only_inside_its_folder(env):
    from api.project_repo_claim import registration_claim

    def claim(path: Path, mode: str = "link", name: str | None = "app"):
        return registration_claim(
            "pa", env["alpha"], mode=mode, path=str(path), name=name, workspace=env["ws"], db=env["db"],
        )

    inside = env["alpha"] / "code" / "app"
    inside.mkdir(parents=True)
    assert claim(inside) == frozenset({"pa"})
    # Case and composition differences still count as inside (shared containment).
    assert claim(env["ws"] / "projects" / "ALPHA" / "code" / "app") == frozenset({"pa"})
    assert claim(env["alpha"], mode="create", name="fresh") == frozenset({"pa"})
    assert claim(env["beta"] / "code") is None
    assert claim(env["ws"] / "shared") is None
    assert claim(env["tmp"] / "elsewhere") is None
    assert claim(Path("relative/app")) is None
    # A symlink inside the folder that resolves outside it claims everywhere.
    (env["alpha"] / "escape").symlink_to(env["tmp"], target_is_directory=True)
    assert claim(env["alpha"] / "escape") is None
    # Another project's registered repo inside this folder claims everywhere.
    env["db"].execute(
        "INSERT INTO project_repo_policies (project_id, repo_name, repo_path, protect_base_checkout) "
        "VALUES ('pb', 'shared', ?, 0)",
        (str(inside),),
    )
    assert claim(inside) is None


# --- An existing entry of the project's source/ folder registers where it is --------


def _with_origin(repo: Path, tmp: Path) -> None:
    origin = tmp / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "push", "origin", "main"], check=True, capture_output=True)


@pytest.mark.asyncio
async def test_native_source_entry_registers_in_place_on_a_busy_instance(env, monkeypatch):
    from tools.project_fence import fence_for_thread
    from tools.worktrees import WorktreeCreateTool

    monkeypatch.setattr("infra.project_repo_policy.WORKSPACE", env["ws"])
    entry = env["alpha"] / "source" / "website"
    _git_repo(entry)
    _with_origin(entry, env["tmp"])
    assert await _call(env["registry"], "q-agent", "hold", {"spawn": True}) == "spawned"
    task = await _held_effect(env, "q-agent-2")
    try:
        result = await _register(env, entry)
    finally:
        await _release(env, task)
    assert "Registered repository 'website'" in result, result
    assert entry.is_dir() and not entry.is_symlink()
    assert sorted(p.name for p in (env["alpha"] / "source").iterdir()) == ["website"]
    row = env["db"].execute("SELECT * FROM project_repo_policies").fetchone()
    assert (row["repo_name"], Path(row["repo_path"])) == ("website", entry.resolve())
    assert (row["pinned_branch"], row["protect_base_checkout"]) == ("main", 1)

    receipt = await WorktreeCreateTool().execute(
        _thread_id="reg", _thread_project_id="pa", repo_name="website",
        branch="feature/x", descriptor="x", base_branch="main",
    )
    assert '"created": true' in receipt, receipt
    assert (entry / ".worktrees" / "reg-x" / "tracked.txt").is_file()

    fence = fence_for_thread("q-agent", db=env["db"])
    assert fence.refusal(entry / "tracked.txt") is not None
    assert fence.refusal(entry / ".worktrees" / "reg-x" / "tracked.txt") is not None


@pytest.mark.asyncio
async def test_linked_source_entry_without_registration_registers_in_place(env):
    outside = env["tmp"] / "checkouts" / "app"
    _git_repo(outside)
    entry = env["alpha"] / "source" / "app"
    entry.symlink_to(outside, target_is_directory=True)
    result = await _register(env, entry)
    assert "Registered repository 'app'" in result, result
    assert entry.is_symlink() and entry.readlink() == outside
    assert sorted(p.name for p in (env["alpha"] / "source").iterdir()) == ["app"]
    row = env["db"].execute("SELECT repo_name, repo_path FROM project_repo_policies").fetchone()
    assert (row["repo_name"], Path(row["repo_path"])) == ("app", outside.resolve())


@pytest.mark.asyncio
async def test_already_registered_entry_is_refused_and_left_alone(env):
    entry = env["alpha"] / "source" / "app"
    _git_repo(entry)
    assert "Registered repository 'app'" in await _register(env, entry)
    result = await _register(env, entry)
    assert "Repository 'app' is already registered for this project" in result, result
    assert entry.is_dir() and (entry / "tracked.txt").is_file()
    assert _registered(env) == 1


@pytest.mark.asyncio
async def test_path_nested_in_a_source_entry_is_refused(env):
    entry = env["alpha"] / "source" / "app"
    nested = entry / "packages" / "lib"
    _git_repo(entry)
    _git_repo(nested)
    result = await _register(env, nested)
    assert "is inside the source entry 'app'; register" in result, result
    assert _registered(env) == 0
    assert entry.is_dir() and nested.is_dir()


@pytest.mark.asyncio
async def test_broken_link_entry_is_refused(env):
    from fastapi import HTTPException

    from api import project_repos

    entry = env["alpha"] / "source" / "gone"
    entry.symlink_to(env["tmp"] / "missing", target_is_directory=True)
    with pytest.raises(HTTPException) as raised:
        project_repos.register_source_repo("pa", project_repos.RepoAdd(mode="link", path=str(entry)))
    assert raised.value.status_code == 400
    assert "is a link that points nowhere" in raised.value.detail
    assert entry.is_symlink()
    assert _registered(env) == 0


@pytest.mark.asyncio
async def test_failed_in_place_registration_leaves_the_entry(env, monkeypatch):
    from api import project_repos

    entry = env["alpha"] / "source" / "app"
    _git_repo(entry)

    def fail(*_args, **_kwargs):
        raise RuntimeError("policy write failed")

    monkeypatch.setattr(project_repos, "register_repo_policy", fail)
    with pytest.raises(RuntimeError, match="policy write failed"):
        project_repos.register_source_repo("pa", project_repos.RepoAdd(mode="link", path=str(entry)))
    assert entry.is_dir() and (entry / "tracked.txt").is_file()
    assert _registered(env) == 0


@pytest.mark.asyncio
async def test_link_entry_resolving_outside_home_is_refused(env, tmp_path_factory):
    from fastapi import HTTPException

    from api import project_repos

    outside_home = tmp_path_factory.mktemp("outside-home") / "app"
    _git_repo(outside_home)
    entry = env["alpha"] / "source" / "app"
    entry.symlink_to(outside_home, target_is_directory=True)
    with pytest.raises(HTTPException) as raised:
        project_repos.register_source_repo("pa", project_repos.RepoAdd(mode="link", path=str(entry)))
    assert raised.value.status_code == 400
    assert raised.value.detail == (
        "source/app links to a folder outside your home folder, which can't be registered"
    )
    assert entry.is_symlink() and entry.readlink() == outside_home
    assert _registered(env) == 0


@pytest.mark.parametrize("entry_name", ["my app", ".cache", "-app"])
@pytest.mark.asyncio
async def test_entry_without_a_safe_repo_name_is_refused(env, entry_name):
    from fastapi import HTTPException

    from api import project_repos
    from api.project_scaffolding import validate_repo_name

    entry = env["alpha"] / "source" / entry_name
    _git_repo(entry)
    with pytest.raises(HTTPException) as raised:
        project_repos.register_source_repo("pa", project_repos.RepoAdd(mode="link", path=str(entry)))
    assert raised.value.status_code == 400
    assert raised.value.detail == validate_repo_name(entry_name)
    assert entry.is_dir() and _registered(env) == 0


@pytest.mark.asyncio
async def test_entry_that_leaves_the_project_after_the_claim_is_refused_to_retry(env, monkeypatch):
    """The claim is read before admission; a folder swapped for an outside link after it fails closed."""
    from fastapi import HTTPException

    from api import project_repos

    entry = env["alpha"] / "source" / "app"
    _git_repo(entry)
    outside = env["tmp"] / "external" / "app"
    _git_repo(outside)
    real = project_repos.resolve_add_source

    def swap_then_resolve(project_id, ws_dir, **kwargs):
        parked = env["tmp"] / "parked-app"
        entry.rename(parked)
        entry.symlink_to(outside, target_is_directory=True)
        return real(project_id, ws_dir, **kwargs)

    monkeypatch.setattr(project_repos, "resolve_add_source", swap_then_resolve)
    with pytest.raises(HTTPException) as raised:
        project_repos.register_source_repo("pa", project_repos.RepoAdd(mode="link", path=str(entry)))
    # Only an add admitted under the narrowed claim checks, so this refusal shows it was.
    assert raised.value.status_code == 409
    assert "no longer inside this project's folder; try again" in raised.value.detail
    assert _registered(env) == 0
    assert entry.is_symlink() and entry.readlink() == outside
    # The retry claims everywhere and registers it, as any outside link.
    monkeypatch.setattr(project_repos, "resolve_add_source", real)
    result = project_repos.register_source_repo("pa", project_repos.RepoAdd(mode="link", path=str(entry)))
    assert Path(result["path"]) == outside.resolve()
    assert _registered(env) == 1


@pytest.mark.asyncio
async def test_link_repointed_after_the_tools_checks_is_refused(env, monkeypatch):
    """The tool registers only the folder its fence and Git-config checks saw."""
    from api import project_repos

    checked = _own_target(env)
    other = env["alpha"] / "code" / "other"
    _git_repo(other)
    entry = env["alpha"] / "source" / "app"
    entry.symlink_to(checked, target_is_directory=True)
    real = project_repos.resolve_add_source

    def repoint_then_resolve(project_id, ws_dir, **kwargs):
        entry.unlink()
        entry.symlink_to(other, target_is_directory=True)
        return real(project_id, ws_dir, **kwargs)

    monkeypatch.setattr(project_repos, "resolve_add_source", repoint_then_resolve)
    result = await _register(env, entry)
    assert result.startswith(
        "Error: The repository's folder changed while it was being registered; "
        "check the path and try again."
    ), result
    assert _registered(env) == 0
    assert entry.is_symlink() and entry.readlink() == other


@pytest.mark.asyncio
async def test_each_transition_logs_the_claim_it_ran_or_was_refused_under(env, caplog):
    import logging

    target = _own_target(env)
    caplog.set_level(logging.INFO, logger="infra.repo_policy_transition")
    task = await _held_effect(env, "p-agent")
    try:
        assert "Repositories can't change" in await _register(env, target)
    finally:
        await _release(env, task)
    assert "Registered repository 'app'" in await _register(env, target)
    messages = [r.getMessage() for r in caplog.records if r.name == "infra.repo_policy_transition"]
    assert messages == [
        "repo policy transition refused claim=own-project blocker_thread=p-agent",
        "repo policy transition admitted claim=own-project",
    ], messages
