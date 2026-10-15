"""Conductor-only instruction-file reads and revision-checked replacements."""

from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

import hashlib
import json

import pytest

from infra.conductor import set_scope_coordinator
from infra.database import get_db
from tests.registry_helpers import make_session
from tools.conductor_instruction_files import (
    ConductorInstructionFileReadTool,
    ConductorInstructionFileReplaceTool,
)


def _setup(tmp_path, monkeypatch):
    import infra.config as config

    workspace = tmp_path / "workspace"
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    db = get_db()
    try:
        db.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)",
            ("other-project", "Other", "t", "t"),
        )
        conductor = make_session(db, prefix="conductor")
        target = make_session(db, prefix="target")
        ordinary = make_session(db, prefix="ordinary")
        foreign = make_session(db, project_id="other-project", prefix="foreign")
        path = workspace / ".threads" / target / "context" / "ROLE.md"
        db.execute(
            "UPDATE threads SET bootloader_files=? WHERE id=?",
            (json.dumps([str(path)]), target),
        )
        db.commit()
        assert set_corsia_agent(conductor, db=db)
    finally:
        db.close()
    path.parent.mkdir(parents=True)
    path.write_text("# Role\nOriginal\n", encoding="utf-8")
    return conductor, target, ordinary, foreign, path


def _audit_rows():
    db = get_db()
    try:
        return db.execute(
            "SELECT actor_user_id,actor_thread_id,target_id,action,files_json,created_at "
            "FROM context_bootloader_events WHERE action='replace_content'"
        ).fetchall()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_conductor_replaces_one_attached_file_and_audits(isolated_db, tmp_path, monkeypatch):
    conductor, target, _, _, path = _setup(tmp_path, monkeypatch)
    read = ConductorInstructionFileReadTool()
    replace = ConductorInstructionFileReplaceTool()

    listing = json.loads(await read.execute(target, _thread_id=conductor))
    assert listing["files"] == [{
        "path": str(path), "size": len(path.read_bytes()),
        "revision": hashlib.sha256(path.read_bytes()).hexdigest(),
    }]
    before = json.loads(await read.execute(target, str(path), _thread_id=conductor))
    result = json.loads(await replace.execute(
        target, str(path), before["revision"], "# Role\nUpdated\n", _thread_id=conductor,
    ))

    assert path.read_text(encoding="utf-8") == "# Role\nUpdated\n"
    assert result["before_revision"] == before["revision"]
    assert result["revision"] != before["revision"]
    rows = _audit_rows()
    assert len(rows) == 1
    assert rows[0]["actor_thread_id"] == conductor
    assert rows[0]["target_id"] == target
    assert rows[0]["created_at"]
    assert json.loads(rows[0]["files_json"]) == [{
        "path": str(path), "before_revision": before["revision"],
        "after_revision": result["revision"],
    }]


@pytest.mark.asyncio
async def test_instruction_replace_refuses_unauthorized_and_foreign_scope(isolated_db, tmp_path, monkeypatch):
    conductor, target, ordinary, foreign, path = _setup(tmp_path, monkeypatch)
    read = ConductorInstructionFileReadTool()
    replace = ConductorInstructionFileReplaceTool()
    before = json.loads(await read.execute(target, str(path), _thread_id=conductor))

    ordinary_result = await replace.execute(
        target, str(path), before["revision"], "unauthorized", _thread_id=ordinary,
    )
    foreign_result = await replace.execute(
        foreign, str(path), before["revision"], "foreign", _thread_id=conductor,
    )

    assert "not the designated conductor" in ordinary_result
    assert "must share a scope" in foreign_result
    assert path.read_text(encoding="utf-8") == "# Role\nOriginal\n"
    assert _audit_rows() == []


@pytest.mark.asyncio
async def test_instruction_replace_refuses_stale_missing_and_unattached(isolated_db, tmp_path, monkeypatch):
    conductor, target, _, _, path = _setup(tmp_path, monkeypatch)
    read = ConductorInstructionFileReadTool()
    replace = ConductorInstructionFileReplaceTool()
    before = json.loads(await read.execute(target, str(path), _thread_id=conductor))
    path.write_text("# Role\nChanged elsewhere\n", encoding="utf-8")

    stale = await replace.execute(
        target, str(path), before["revision"], "stale", _thread_id=conductor,
    )
    assert "changed since it was read" in stale
    assert path.read_text(encoding="utf-8") == "# Role\nChanged elsewhere\n"
    path.unlink()
    missing = await replace.execute(
        target, str(path), before["revision"], "missing", _thread_id=conductor,
    )
    unattached = await replace.execute(
        target, str(path.with_name("OTHER.md")), before["revision"], "other", _thread_id=conductor,
    )

    assert "attached instruction file is missing" in missing
    assert "not attached" in unattached
    assert _audit_rows() == []


@pytest.mark.asyncio
async def test_stale_refusal_never_writes_the_file(isolated_db, tmp_path, monkeypatch):
    from pathlib import Path

    conductor, target, _, _, path = _setup(tmp_path, monkeypatch)
    before = json.loads(await ConductorInstructionFileReadTool().execute(
        target, str(path), _thread_id=conductor,
    ))
    path.write_text("# Role\nChanged elsewhere\n", encoding="utf-8")
    writes: list[Path] = []
    real_write = Path.write_bytes
    monkeypatch.setattr(
        Path, "write_bytes", lambda self, data: writes.append(self) or real_write(self, data),
    )

    stale = await ConductorInstructionFileReplaceTool().execute(
        target, str(path), before["revision"], "stale", _thread_id=conductor,
    )

    assert "changed since it was read" in stale
    assert writes == []


@pytest.mark.asyncio
async def test_an_audit_failure_after_the_save_is_reported_not_hidden(isolated_db, tmp_path, monkeypatch):
    import tools.conductor_instruction_files as module

    conductor, target, _, _, path = _setup(tmp_path, monkeypatch)
    before = json.loads(await ConductorInstructionFileReadTool().execute(
        target, str(path), _thread_id=conductor,
    ))

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(module, "log_bootloader_event", fail_audit)

    result = json.loads(await ConductorInstructionFileReplaceTool().execute(
        target, str(path), before["revision"], "# Role\nUpdated\n", _thread_id=conductor,
    ))
    assert path.read_text(encoding="utf-8") == "# Role\nUpdated\n"
    assert "audit unavailable" in result["audit_error"]
    assert _audit_rows() == []


@pytest.mark.asyncio
async def test_replace_is_a_guidance_save_committed_as_the_conductor(isolated_db, tmp_path, monkeypatch):
    import subprocess

    conductor, target, _, _, path = _setup(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "init", "--quiet"], cwd=workspace, check=True)
    subprocess.run([*git, "commit", "--allow-empty", "-qm", "root"], cwd=workspace, check=True)
    before = json.loads(await ConductorInstructionFileReadTool().execute(
        target, str(path), _thread_id=conductor,
    ))

    result = json.loads(await ConductorInstructionFileReplaceTool().execute(
        target, str(path), before["revision"], "# Role\nUpdated\n",
        reason="Narrow the role", evidence="issue-913", _thread_id=conductor,
    ))

    assert "Saved and committed" in result["guidance_history"]
    log = subprocess.run(
        ["git", "log", "-1", "--format=%ae%n%B"], cwd=workspace, check=True, capture_output=True, text=True,
    ).stdout
    assert log.startswith(f"{conductor}@agents.corsia.local")
    assert "Reason: Narrow the role" in log and "Evidence: issue-913" in log
    shown = subprocess.run(
        ["git", "show", f"HEAD:{path.relative_to(workspace).as_posix()}"],
        cwd=workspace, check=True, capture_output=True, text=True,
    ).stdout
    assert shown == "# Role\nUpdated\n"
