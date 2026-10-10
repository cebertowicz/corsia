"""Instruction creation and actual next-turn responsibility/skill delivery."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.bootloader_management import thread_bootloader_files
from core.skills_registry import resolve_effective_skills
from infra.conductor import set_conductor
from infra.database import get_db
from tests.registry_helpers import make_session
from tools.conductor_instruction_create import ConductorInstructionFileCreateTool

SKILL = """---
name: agent-check
description: Use when checking the installed agent.
corsia:
  handler:
    kind: instruction
---
Read scripts/check.py for the deterministic check.
"""


def _setup(tmp_path, monkeypatch):
    import infra.config as config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    db = get_db()
    try:
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('other','Other','t','t')")
        db.commit()
        actor = make_session(db, prefix="conductor")
        target = make_session(db, prefix="agent")
        ordinary = make_session(db, prefix="ordinary")
        foreign = make_session(db, prefix="foreign", project_id="other")
        assert set_conductor("workspace", actor, db=db)
        return workspace, actor, target, ordinary, foreign
    finally:
        db.close()


def test_bundle_revision_distinguishes_support_directories():
    from tools.conductor_instruction_create import _bundle_revision

    script = Path("skills/check/scripts/check.txt")
    reference = Path("skills/check/references/check.txt")
    assert _bundle_revision({script: b"a", reference: b"b"}) != _bundle_revision({script: b"b", reference: b"a"})


def test_undelivered_responsibility_names_its_missing_attachment():
    from tools.conductor_instruction_create import _undelivered_error

    assert _undelivered_error("responsibility") == (
        "Saved but not delivered: the bootloader attachment was not included; "
        "read the destination before retrying"
    )
    assert "skill is shadowed" in _undelivered_error("skill")


@pytest.mark.asyncio
async def test_unexpected_install_failure_is_logged_and_tells_caller_to_read_first(monkeypatch, caplog):
    import tools.conductor_instruction_create as instruction_create

    async def fails(*args, **kwargs):
        raise RuntimeError("install-sentinel")

    monkeypatch.setattr(instruction_create, "run_in_db", fails)
    result = await ConductorInstructionFileCreateTool().execute(
        "target", "responsibility", "absent", "# Responsibility\n", _thread_id="actor",
    )
    assert result == (
        "Error: instruction install failed; read the destination before retrying because files may already be saved."
    )
    assert any(record.getMessage() == "Conductor instruction install failed" for record in caplog.records)


@pytest.mark.asyncio
async def test_create_attach_idempotent_retry_and_revision(isolated_db, tmp_path, monkeypatch):
    workspace, actor, target, _, _ = _setup(tmp_path, monkeypatch)
    tool = ConductorInstructionFileCreateTool()
    content = "# Responsibility\nOwn the authorized checks.\n"
    arguments = dict(target_thread_id=target, kind="responsibility", expected_revision="absent",
                     content=content, _thread_id=actor)
    receipt = json.loads(await tool.execute(**arguments))
    assert receipt["ok"], receipt
    assert receipt["delivery"] == {"next_turn": True, "surface": "bootloader"}
    path = Path(receipt["path"])
    assert path.read_text() == content
    db = get_db()
    try:
        assert str(path) in thread_bootloader_files(target, db=db)
        before = db.execute("SELECT count(*) FROM context_bootloader_events").fetchone()[0]
    finally:
        db.close()
    retry = json.loads(await tool.execute(**arguments))
    assert not retry["changed"]
    db = get_db()
    try:
        assert db.execute("SELECT count(*) FROM context_bootloader_events").fetchone()[0] == before
    finally:
        db.close()
    stale = await tool.execute(**{**arguments, "content": content + "Changed\n"})
    assert "changed" in stale and receipt["revision"] in stale
    assert path.read_text() == content
    changed = json.loads(await tool.execute(**{**arguments, "expected_revision": receipt["revision"], "content": content + "Changed\n"}))
    assert changed["ok"] and changed["revision"] != receipt["revision"]


@pytest.mark.asyncio
async def test_skill_and_script_install_reaches_effective_next_turn_list(isolated_db, tmp_path, monkeypatch):
    workspace, actor, target, _, _ = _setup(tmp_path, monkeypatch)
    receipt = json.loads(await ConductorInstructionFileCreateTool().execute(
        target, "skill", "absent", SKILL, name="agent-check",
        supporting_files={"scripts/check.py": "print('check')\n"}, _thread_id=actor,
    ))
    assert receipt["ok"] and receipt["delivery"]["next_turn"], receipt
    script = Path(receipt["supporting_files"][0])
    assert script.read_text() == "print('check')\n"
    db = get_db()
    try:
        skills = resolve_effective_skills(thread_id=target, project_id=None, workspace=workspace, db=db)
        assert any(row.name == "agent-check" and row.path == Path(receipt["path"]) for row in skills.included)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_untrusted_actor_and_foreign_scope_cannot_install(isolated_db, tmp_path, monkeypatch):
    workspace, actor, target, ordinary, foreign = _setup(tmp_path, monkeypatch)
    tool = ConductorInstructionFileCreateTool()
    for caller, destination in ((ordinary, target), (actor, foreign)):
        result = await tool.execute(destination, "responsibility", "absent", "# Role\n", _thread_id=caller)
        assert result.startswith("Error:"), result
    assert not (workspace / ".threads" / target / "context" / "RESPONSIBILITY.md").exists()


@pytest.mark.asyncio
async def test_invalid_skill_and_support_path_do_not_write(isolated_db, tmp_path, monkeypatch):
    workspace, actor, target, _, _ = _setup(tmp_path, monkeypatch)
    tool = ConductorInstructionFileCreateTool()
    for name, text, supports in (("agent-check", "missing frontmatter", {}),
                                  ("agent-check", SKILL, {"scripts/../../outside": "bad"}),
                                  ("../outside", SKILL, {})):
        result = await tool.execute(target, "skill", "absent", text, name=name,
                                    supporting_files=supports, _thread_id=actor)
        assert result.startswith("Error:"), result
    assert not (workspace / ".threads" / target / "skills").exists()
