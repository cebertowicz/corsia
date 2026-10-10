from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from infra.platform_paths import WorkspaceSelection
from tests.registry_helpers import make_session

_VALID = "---\nname: demo\ndescription: Use when testing saves.\n---\n# Demo\n\nBody text.\n"
_INVALID = "---\nname: demo\ndescription: Use when testing: invalid YAML.\n---\n# Demo\n"


def _git(workspace: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.fixture
def skill_save_scope(isolated_db, tmp_path, monkeypatch):
    from infra import config
    from tools import filesystem_edit, filesystem_rw, host_access_gate
    from tools.filesystem_gate import AuthorizedPath

    async def allow_file_action(**_kwargs):
        return host_access_gate.HostActionAuthorization()

    async def allow_file_path(*, params, **_kwargs):
        return AuthorizedPath(Path(params["path"]), reviewed=False)

    monkeypatch.setattr(host_access_gate, "authorize_host_action", allow_file_action)
    monkeypatch.setattr(filesystem_rw, "_resolve_path_for_action", allow_file_path)
    monkeypatch.setattr(filesystem_edit, "_resolve_path_for_action", allow_file_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--quiet")
    _git(workspace, "config", "user.email", "test@corsia.local")
    _git(workspace, "config", "user.name", "Test")
    _git(workspace, "commit", "--allow-empty", "-qm", "root")
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "WORKSPACE_SELECTION", WorkspaceSelection("skill-save", workspace.resolve(), "Tests"))
    from infra.database import get_db

    db = get_db()
    thread_id = make_session(db, title="Skill saver", prefix="skill-save")
    db.close()
    skill = workspace / ".threads" / thread_id / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(_VALID, encoding="utf-8")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-qm", "valid skill")
    return workspace, thread_id, skill


@pytest.mark.asyncio
async def test_write_edit_and_multi_refuse_invalid_skill_without_bytes_or_history(skill_save_scope):
    from tools.filesystem_edit import EditFileTool, MultiEditTool
    from tools.filesystem_rw import WriteFileTool

    workspace, thread_id, skill = skill_save_scope
    before, head = skill.read_bytes(), _git(workspace, "rev-parse", "HEAD")
    write = await WriteFileTool(workspace=workspace).execute(str(skill), _INVALID, _thread_id=thread_id)
    edit = await EditFileTool(workspace=workspace).execute(
        str(skill), "Use when testing saves.", "", _thread_id=thread_id,
    )
    multi = await MultiEditTool(workspace=workspace).execute(
        str(skill), [{"old_string": "name: demo", "new_string": "name:"}], _thread_id=thread_id,
    )

    from core.skill_validation import validate_skill_text

    expected_write = (
        "Error: SKILL.md not saved — it would not load as a skill. The file is unchanged.\n"
        f"{skill}\n- {validate_skill_text(_INVALID).errors[0]}\n"
        "Fix: correct the YAML frontmatter at the reported location. Quote text values containing ': ' "
        "or use a block scalar where needed, then save again."
    )
    assert write == expected_write
    for result in (edit, multi):
        assert result.startswith("Error: SKILL.md not saved — it would not load as a skill. The file is unchanged.\n")
        assert f"\n{skill}\n- " in result
    assert skill.read_bytes() == before
    assert _git(workspace, "rev-parse", "HEAD") == head


@pytest.mark.asyncio
async def test_already_invalid_skill_edit_is_refused(skill_save_scope):
    from tools.filesystem_edit import EditFileTool

    workspace, thread_id, skill = skill_save_scope
    skill.write_text(_INVALID, encoding="utf-8")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-qm", "invalid skill")
    before, head = skill.read_bytes(), _git(workspace, "rev-parse", "HEAD")

    result = await EditFileTool(workspace=workspace).execute(
        str(skill), "# Demo", "# Still broken", _thread_id=thread_id,
    )

    assert result.startswith("Error: SKILL.md not saved")
    assert skill.read_bytes() == before
    assert _git(workspace, "rev-parse", "HEAD") == head


def test_placement_template_and_parsed_description_support_yaml_scalars():
    from core.placement_decide import append_to_skill, skill_description
    from core.skill_validation import validate_skill_text

    description = "Use when reviewing evidence: verify the rendered result."
    created = append_to_skill(None, "example", "Check the output.", description)
    block = "---\nname: example\ndescription: >-\n  First line\n  second line\n---\n# Example\n"

    assert validate_skill_text(created).ok
    assert skill_description(created) == description
    assert skill_description(block) == "First line second line"


def test_skills_index_lists_errors_even_without_valid_skills_and_omits_shadowed(tmp_path):
    from core.skill_registry_types import SkillRow
    from core.skills_registry import _skills_index_content

    broken = SkillRow(
        "broken", "", "thread", "t", tmp_path / "SKILL.md", tmp_path / "SKILL.md", {}, "error",
        ["description is required", "name is required"],
    )
    shadowed = SkillRow("shadowed", "", "thread", "t", tmp_path / "other.md", tmp_path / "other.md", {}, "error", ["shadowed by System"])
    content = _skills_index_content([], ["system-copy"], errors=[broken])

    assert "Not loaded — skill errors (these sources are skipped until repaired):" in content
    assert f"broken (thread, {tmp_path / 'SKILL.md'}):\n  - description is required\n  - name is required" in content
    assert "shadowed (" not in content
    assert "system-copy" in content
    assert _skills_index_content([], [], errors=[]).startswith("Available skills.")


def test_placement_refusal_records_validation_errors(tmp_path, monkeypatch):
    from core import placement_outcomes
    from core.placement_decide import Decision, Destination, ThreadPlace
    from infra import config
    from infra.guidance_git import SaveResult
    from infra.skill_save_validation import INVALID_SKILL_REASON, REFUSED
    import infra.guidance_git as guidance_git

    captured: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(guidance_git, "workspace_repository", lambda *_args, **_kwargs: (tmp_path, None))
    monkeypatch.setattr(guidance_git, "find_commit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        guidance_git,
        "save_guidance",
        lambda *_args, **_kwargs: SaveResult(REFUSED, INVALID_SKILL_REASON, errors=("description is required",)),
    )
    monkeypatch.setattr(
        placement_outcomes,
        "settle",
        lambda _entry, outcome, reason, **kwargs: captured.append((outcome, reason, kwargs.get("detail"))),
    )
    path = tmp_path / "skills" / "demo" / "SKILL.md"
    decision = Decision("agent", "task_start", None, "Check output.", "demo", "Use when checking.", None, "Needed.")
    place = ThreadPlace("thread", "Agent", None, True, tmp_path, None)
    entry = {
        "thread_id": "thread", "entry_id": 1, "revision": 1, "history_epoch": 1,
        "body": {"statement": "Check output."},
    }

    placement_outcomes.write(entry, place, decision, Destination("skill", path))

    assert captured == [("unplaced", "invalid-skill", "description is required")]


def test_lesson_refusal_records_the_same_outcome_shape_without_writing(tmp_path, monkeypatch):
    from core import lesson_outcomes, placement_outcomes
    from core.placement_decide import Decision, ThreadPlace
    from infra import config

    captured: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(
        placement_outcomes,
        "settle",
        lambda _entry, outcome, reason, **kwargs: captured.append((outcome, reason, kwargs.get("detail"))),
    )
    path = tmp_path / "skills" / "demo" / "SKILL.md"
    decision = Decision("agent", "task_start", None, "Check output.", "demo", "Use when checking.", None, "Needed.")
    place = ThreadPlace("thread", "Agent", None, True, tmp_path, None)
    entry = {"thread_id": "thread", "entry_id": 1, "revision": 1, "history_epoch": 1, "body": {"statement": "Check output."}}

    saved = lesson_outcomes._save(
        entry, place, decision, "skills/demo/SKILL.md", {path: _INVALID}, {path: None}, "trial",
    )

    assert saved is False
    assert not path.exists()
    assert len(captured) == 1
    outcome, reason, detail = captured[0]
    assert (outcome, reason) == ("unplaced", "invalid-skill")
    assert detail and detail.startswith("frontmatter parse failed:")


def test_broken_resolved_thread_skill_is_noticed_then_disappears_when_fixed(isolated_db, tmp_path):
    from core.skills_registry import skill_context_sources
    from infra.database import get_db

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = get_db()
    thread_id = make_session(db, title="Skill delivery", prefix="skill-delivery")
    db.close()
    skill = workspace / ".threads" / thread_id / "skills" / "broken" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: broken\n---\n# Broken\n", encoding="utf-8")

    db = get_db()
    try:
        broken_sources = skill_context_sources(thread_id=thread_id, project_id=None, workspace=workspace, db=db)
    finally:
        db.close()
    broken_index = next(source for source in broken_sources if source.source_id == "dynamic:skills-index")
    assert broken_index.inclusion == "included"
    assert "Not loaded — skill errors (these sources are skipped until repaired):" in (broken_index.prompt_content or "")
    assert "broken (" in (broken_index.prompt_content or "")

    skill.write_text(_VALID, encoding="utf-8")
    db = get_db()
    try:
        fixed_sources = skill_context_sources(thread_id=thread_id, project_id=None, workspace=workspace, db=db)
    finally:
        db.close()
    fixed_index = next(source for source in fixed_sources if source.source_id == "dynamic:skills-index")
    assert "Not loaded — skill errors (these sources are skipped until repaired):" not in (fixed_index.prompt_content or "")
    assert "demo: Use when testing saves." in (fixed_index.prompt_content or "")
