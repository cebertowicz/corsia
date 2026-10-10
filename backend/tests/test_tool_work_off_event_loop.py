"""Tool execution keeps its blocking work off the server event loop.

Each case runs a real tool path against a registered, branch-pinned repository,
so repository policy reads its database rows and runs Git exactly as it does in
production. Only the final process spawn is stubbed. Every process spawn,
filesystem call and SQLite checkout on the event-loop thread is a failure.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from tests.project_ref_helpers import project_ref, select_workspace

import pytest

from tests.event_loop_blocking import blocking_work_on_event_loop

THREAD = "loop-thread"
TURN = "loop-turn"


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True, text=True)


def _pinned_repository(tmp_path: Path, monkeypatch, isolated_db) -> Path:
    """Register one branch-pinned repository under an isolated workspace."""
    import infra.config as config
    import infra.project_repo_policy as project_repo_policy
    from tools import sandbox_config

    workspace = (tmp_path / "workspace").resolve()
    repo = (tmp_path / "repo").resolve()
    _git("-c", "init.defaultRefFormat=files", "init", "-b", "main", str(repo))
    _git("-C", str(repo), "-c", "user.name=CORSIA Test", "-c",
         "user.email=test@corsia.local", "commit", "--allow-empty", "-m", "initial")
    (repo / "README.md").write_text("pinned\n", encoding="utf-8")
    project = workspace / "projects" / "p1"
    (project / "source").mkdir(parents=True)
    (project / "source" / "app").symlink_to(repo, target_is_directory=True)
    for module in (config, project_repo_policy, sandbox_config):
        monkeypatch.setattr(module, "WORKSPACE", workspace)
    select_workspace(monkeypatch, workspace)
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [workspace])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    db = sqlite3.connect(isolated_db)
    db.execute(
        "INSERT INTO projects (id,name,path,created_at,updated_at) VALUES ('p1','P1',?,'t','t')",
        (project_ref("p1"),),
    )
    db.execute(
        "INSERT INTO project_repo_policies (project_id,repo_name,repo_path,pinned_branch,"
        "protect_base_checkout) VALUES ('p1','app',?,'main',1)",
        (str(repo),),
    )
    db.execute(
        "INSERT INTO threads (id,user_id,model,project_id,created_at,updated_at) "
        "VALUES (?,'loop-user','test/model','p1','t','t')",
        (THREAD,),
    )
    db.commit()
    db.close()
    policies = project_repo_policy.list_registered_repo_policies()
    assert [(p.repo_name, p.pinned_branch, p.enforcement_error) for p in policies] == [
        ("app", "main", None)
    ]
    return repo


@pytest.mark.asyncio
async def test_host_action_authorization_evaluates_repository_policy_off_the_loop(
    isolated_db, tmp_path, monkeypatch,
):
    from tools.filesystem_rw import ReadFileTool
    from tools.host_access_gate import authorize_host_action

    repo = _pinned_repository(tmp_path, monkeypatch, isolated_db)

    with blocking_work_on_event_loop(monkeypatch) as blocking:
        await authorize_host_action(
            thread_id=THREAD, project_id="p1", tool=ReadFileTool(),
            tool_name="read_file", params={"path": str(repo / "README.md")},
        )

    assert blocking == [], "\n".join(blocking)


@pytest.mark.asyncio
async def test_file_reads_run_off_the_loop(isolated_db, tmp_path, monkeypatch):
    from tools.filesystem_edit import ListDirTool
    from tools.filesystem_rw import ReadFileTool

    repo = _pinned_repository(tmp_path, monkeypatch, isolated_db)
    identity = {"_thread_id": THREAD, "_thread_project_id": "p1"}

    with blocking_work_on_event_loop(monkeypatch) as blocking:
        read = await ReadFileTool().execute(path=str(repo / "README.md"), **identity)
        listed = await ListDirTool().execute(path=str(repo), **identity)

    assert "pinned" in read
    assert "f README.md" in listed
    assert blocking == [], "\n".join(blocking)


@pytest.mark.asyncio
async def test_file_writes_resolve_their_path_off_the_loop(isolated_db, tmp_path, monkeypatch):
    """Path policy runs off the loop; the write itself stays with its result.

    A write that crossed an await would let a cancellation land after the file
    changed and before the tool returned its result, dropping that result.
    """
    from tools.filesystem_edit import EditFileTool
    from tools.filesystem_rw import WriteFileTool

    _pinned_repository(tmp_path, monkeypatch, isolated_db)
    workspace_file = tmp_path / "workspace" / "projects" / "p1" / ".threads" / THREAD / "notes.md"
    identity = {"_thread_id": THREAD, "_thread_project_id": "p1"}

    with blocking_work_on_event_loop(monkeypatch) as blocking:
        written = await WriteFileTool().execute(
            path=str(workspace_file), content="first\n", **identity,
        )
        edited = await EditFileTool().execute(
            path=str(workspace_file), old_string="first", new_string="second", **identity,
        )

    assert not written.startswith("Error"), written
    assert not edited.startswith("Error"), edited
    assert workspace_file.read_text(encoding="utf-8") == "second\n"
    before_effect = [
        entry for entry in blocking
        if "tools/filesystem_gate.py" in entry or "tools/host_access_gate.py" in entry
    ]
    assert before_effect == [], "\n".join(before_effect)
    assert all(
        "tools/filesystem_rw.py" in entry or "tools/filesystem_edit.py" in entry
        for entry in blocking
    ), "\n".join(blocking)


@pytest.mark.asyncio
async def test_shell_prepares_its_envelope_off_the_loop(isolated_db, tmp_path, monkeypatch):
    from core.turn_core import TurnIdentity
    from host_access import processes
    from tools import shell

    repo = _pinned_repository(tmp_path, monkeypatch, isolated_db)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_safehouse = bin_dir / "safehouse"
    fake_safehouse.write_text("#!/bin/sh\necho 'Agent Safehouse 0.11.1'\n", encoding="utf-8")
    fake_safehouse.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    spawned: list[tuple[str, ...]] = []

    async def spawn(*argv, **_kwargs):
        spawned.append(argv)
        return _FinishedProcess()

    monkeypatch.setattr(shell.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(processes, "_process_creation_fingerprint", lambda _pid: "fingerprint")
    monkeypatch.setattr(processes.PROCESS_OWNER, "process_dir", tmp_path / "processes")

    with blocking_work_on_event_loop(monkeypatch) as blocking:
        result = await shell.BashTool(working_dir=str(repo)).execute(
            "git status", _thread_id=THREAD, _thread_project_id="p1",
            _host_access_turn_identity=TurnIdentity(TURN, 1),
        )

    assert result == "clean", result
    assert spawned and spawned[0][0] == str(fake_safehouse.resolve())
    # The process owner writes the capped foreground log on the loop, so a
    # cancellation cannot separate it from the process's terminal bookkeeping.
    owner_log = [entry for entry in blocking if "_write_foreground_log" in entry]
    assert len(owner_log) == 1, owner_log
    assert [entry for entry in blocking if entry not in owner_log] == [], "\n".join(blocking)


class _FinishedProcess:
    """A process that already exited cleanly with fixed output."""

    pid = 424242
    returncode = 0

    async def communicate(self):
        return b"clean", b""

    async def wait(self):
        return 0


@pytest.mark.asyncio
async def test_harness_tool_result_persists_off_the_loop(isolated_db, monkeypatch):
    from core.turn_core import persist_turn_start
    from infra.database import get_db
    from infra.thread_driver import admit_input
    from subagents import harness_tool_bridge
    from subagents.child_thread import persist_assistant_step
    from tools.base import Tool
    from tools.registry import ToolRegistry

    class _Echo(Tool):
        name = "echo"
        description = "Return a fixed result."
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **_kwargs):
            return "echoed"

    db = sqlite3.connect(isolated_db)
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?,'loop-user','test/model','t','t')",
        (THREAD,),
    )
    db.commit()
    db.close()
    connection = get_db()
    admission = admit_input(
        THREAD, role="user", content="go", intent="apply_now", message_id="loop-input",
        turn_id=TURN, boot_epoch="boot", admitted_at="t", db=connection,
    )
    assert admission.batch is not None
    identity = persist_turn_start(TURN, THREAD, "test/model", "go", db=connection)
    step_id = persist_assistant_step(
        THREAD, content="", tool_uses=[{"id": "call-1", "name": "echo", "arguments": {}}],
        turn_id=identity.turn_id, db=connection,
    )
    connection.commit()
    connection.close()
    registry = ToolRegistry()
    registry.register(_Echo())

    with blocking_work_on_event_loop(monkeypatch) as blocking:
        result = await harness_tool_bridge.execute_tool(
            registry, thread_id=THREAD, turn_identity=identity, invocation_id="invocation-1",
            provider_tool_call_id="call-1", assistant_step_id=step_id, name="echo", arguments={},
        )

    assert "echoed" in str(result)
    assert blocking == [], "\n".join(blocking)


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_fails", (False, True))
async def test_cancelled_envelope_preparation_removes_the_profile_it_wrote(
    tmp_path, monkeypatch, caplog, worker_fails,
):
    import asyncio
    import tempfile
    import threading

    from tools.shell_envelope import Envelope, prepare_execution_envelope

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    building = threading.Event()
    release = threading.Event()

    def build_profile(*_args, **_kwargs):
        building.set()
        release.wait(5)
        if worker_fails:
            raise RuntimeError("policy read failed")
        return "(version 1)"

    envelope = Envelope(build_profile, lambda command, *_a, **_k: ["safehouse", "--", command])
    preparing = asyncio.ensure_future(prepare_execution_envelope("true", tmp_path, envelope))
    await asyncio.to_thread(building.wait, 5)
    preparing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await preparing
    assert list(tmp_path.glob("corsia-shell-envelope-*.sb"))
    release.set()

    for _ in range(200):
        if not list(tmp_path.glob("corsia-shell-envelope-*.sb")):
            break
        await asyncio.sleep(0.01)
    assert list(tmp_path.glob("corsia-shell-envelope-*.sb")) == []
    for _ in range(200):
        if not worker_fails or "after its caller was cancelled" in caplog.text:
            break
        await asyncio.sleep(0.01)
    assert ("after its caller was cancelled" in caplog.text) is worker_fails
