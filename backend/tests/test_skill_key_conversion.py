"""The one-time startup rewrite of the retired skill frontmatter key ``maestro:`` to ``corsia:``."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import get_args

import pytest
from loguru import logger

from core.role_context_sources import RoleName
from core.skill_validation import validate_skill_text
from infra.settings import get_setting
from tests.project_ref_helpers import project_ref, select_workspace

# The body repeats the retired key at column 0 inside an example block, and ends without a
# newline, so any rewrite that strays past the frontmatter changes the body's bytes.
BODY = (
    "\n# Sample\n\nCopy this template:\n\n```yaml\n---\nname: example\n"
    "maestro:\n  handler:\n    kind: instruction\n---\n```\n\ntrailing space   \nñ"
)
LEGACY = (
    "---\nname: sample\ndescription: A skill written before the rename\n"
    "maestro:  # kept comment\n  handler:\n    kind: instruction\n"
    "  workflow:\n    steps:\n      - {id: build, required: true, check: Build passes}\n---"
    + BODY
)
BOTH = "---\nname: both\ndescription: Both keys\nmaestro:\n  handler: {kind: cli}\ncorsia:\n  handler: {kind: instruction}\n---\n# Both\n"
MISFILED = "maestro.handler is CORSIA metadata, which no reader reads under maestro; move it to corsia.yaml beside SKILL.md"
RETIRED = "the top-level corsia block is retired for handler and workflow data"


def _db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT)")
    connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    return connection


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _workspace(tmp_path: Path, monkeypatch) -> tuple[Path, Path, sqlite3.Connection]:
    workspace = tmp_path / "workspace"
    project = workspace / "projects" / "sample"
    project.mkdir(parents=True)
    select_workspace(monkeypatch, workspace)
    db = _db()
    db.execute(
        "INSERT INTO projects (id, name, path) VALUES (?, ?, ?)",
        ("proj-1", "Sample", project_ref("sample")),
    )
    return workspace, project, db


def test_rewrites_only_the_frontmatter_key_and_keeps_the_body_byte_identical(tmp_path, monkeypatch):
    from core.skill_key_conversion import convert_legacy_skill_keys

    workspace, _project, db = _workspace(tmp_path, monkeypatch)
    skill = _write(workspace / "skills" / "sample" / "SKILL.md", LEGACY)

    result = convert_legacy_skill_keys(workspace, db=db)

    converted = skill.read_text(encoding="utf-8")
    assert result is not None and result.converted == 1 and result.refused == 0
    assert converted == LEGACY.replace("maestro:  # kept comment", "corsia:  # kept comment", 1)
    assert converted.endswith(BODY) and converted.count("\nmaestro:\n") == 1
    before, after = validate_skill_text(LEGACY), validate_skill_text(converted)
    assert after.body == before.body
    assert after.metadata["corsia"] == before.metadata["maestro"]
    assert "maestro" not in after.metadata


def test_a_converted_file_no_longer_matches_the_rewrite():
    from core.skill_key_conversion import rewrite_skill_key

    converted, reason = rewrite_skill_key(LEGACY)

    assert reason is None and converted != LEGACY
    assert rewrite_skill_key(converted) == (converted, None)


def test_the_reader_sees_the_renamed_block_only_after_it_is_converted(tmp_path, monkeypatch):
    from core.skill_key_conversion import convert_legacy_skill_keys

    workspace, _project, db = _workspace(tmp_path, monkeypatch)
    skill = _write(workspace / "skills" / "sample" / "SKILL.md", LEGACY)

    before = validate_skill_text(LEGACY)
    convert_legacy_skill_keys(workspace, db=db)
    after = validate_skill_text(skill.read_text(encoding="utf-8"))

    assert MISFILED in before.errors and not any(RETIRED in error for error in before.errors)
    assert [error for error in after.errors if RETIRED in error] and MISFILED not in after.errors


def test_the_pass_runs_once_per_install_and_later_files_fail_validation(tmp_path, monkeypatch):
    from core.skill_key_conversion import COMPLETED_SETTING, convert_legacy_skill_keys

    workspace, _project, db = _workspace(tmp_path, monkeypatch)
    skill = _write(workspace / "skills" / "sample" / "SKILL.md", LEGACY)
    first = convert_legacy_skill_keys(workspace, db=db)
    once, stamp = skill.read_bytes(), skill.stat().st_mtime_ns
    late = _write(workspace / "skills" / "late" / "SKILL.md", LEGACY)

    again = convert_legacy_skill_keys(workspace, db=db)

    assert first is not None and first.converted == 1
    assert get_setting(COMPLETED_SETTING, db=db) is not None
    assert again is None
    assert skill.read_bytes() == once and skill.stat().st_mtime_ns == stamp
    assert late.read_text(encoding="utf-8") == LEGACY
    assert MISFILED in validate_skill_text(LEGACY).errors


def test_a_pass_that_fails_is_not_recorded_and_runs_again(tmp_path, monkeypatch):
    from core import skill_key_conversion

    workspace, _project, db = _workspace(tmp_path, monkeypatch)
    skill = _write(workspace / "skills" / "sample" / "SKILL.md", LEGACY)
    real_skill_files = skill_key_conversion.skill_files

    def fail(_root):
        raise OSError("volume went away")

    monkeypatch.setattr(skill_key_conversion, "skill_files", fail)
    with pytest.raises(OSError, match="volume went away"):
        skill_key_conversion.convert_legacy_skill_keys(workspace, db=db)
    assert get_setting(skill_key_conversion.COMPLETED_SETTING, db=db) is None

    monkeypatch.setattr(skill_key_conversion, "skill_files", real_skill_files)
    result = skill_key_conversion.convert_legacy_skill_keys(workspace, db=db)

    assert result is not None and result.converted == 1
    assert "corsia" in validate_skill_text(skill.read_text(encoding="utf-8")).metadata


def test_a_pass_with_an_unresolved_root_is_not_recorded_and_converts_it_later(tmp_path, monkeypatch):
    from core import skill_key_conversion

    workspace, project, db = _workspace(tmp_path, monkeypatch)
    role_skill = _write(project / "context" / "roles" / "agent" / "skills" / "sample" / "SKILL.md", LEGACY)
    real_role_directory = skill_key_conversion.role_directory

    def unresolved(scope_root, role):
        raise RuntimeError("role context container cannot be a symlink")

    monkeypatch.setattr(skill_key_conversion, "role_directory", unresolved)
    first = skill_key_conversion.convert_legacy_skill_keys(workspace, db=db)

    assert first is not None and first.unresolved_roots == len(get_args(RoleName)) * 2 and first.converted == 0
    assert get_setting(skill_key_conversion.COMPLETED_SETTING, db=db) is None
    assert role_skill.read_text(encoding="utf-8") == LEGACY

    monkeypatch.setattr(skill_key_conversion, "role_directory", real_role_directory)
    second = skill_key_conversion.convert_legacy_skill_keys(workspace, db=db)

    assert second is not None and second.unresolved_roots == 0 and second.converted == 1
    assert get_setting(skill_key_conversion.COMPLETED_SETTING, db=db) is not None
    assert "corsia" in validate_skill_text(role_skill.read_text(encoding="utf-8")).metadata


def test_a_file_with_both_keys_is_refused_logged_and_left_unchanged(tmp_path, monkeypatch):
    from core.skill_key_conversion import convert_legacy_skill_keys

    workspace, _project, db = _workspace(tmp_path, monkeypatch)
    skill = _write(workspace / "skills" / "both" / "SKILL.md", BOTH)
    lines: list[str] = []
    sink = logger.add(lines.append, format="{level}|{message}", level="WARNING")
    try:
        result = convert_legacy_skill_keys(workspace, db=db)
    finally:
        logger.remove(sink)

    assert result is not None and result.converted == 0 and result.refused == 1
    assert skill.read_text(encoding="utf-8") == BOTH
    assert any(line.startswith("WARNING|") and str(skill) in line and "both" in line for line in lines), lines
    assert MISFILED in validate_skill_text(BOTH).errors


def test_every_registry_root_converts_and_the_system_projection_does_not(tmp_path, monkeypatch):
    from core.skill_key_conversion import convert_legacy_skill_keys

    workspace, project, db = _workspace(tmp_path, monkeypatch)
    discovered = [
        _write(root / rel / "SKILL.md", LEGACY)
        for root in (workspace, project)
        for rel in (
            "skills/sample",
            "context/roles/agent/skills/sample",
            "context/roles/conductor/skills/sample",
            ".threads/thread-1/skills/sample",
        )
    ]
    projection = _write(workspace / ".corsia" / "skills" / "sample" / "SKILL.md", LEGACY)
    undiscovered = _write(project / "working" / "skills" / "sample" / "SKILL.md", LEGACY)

    result = convert_legacy_skill_keys(workspace, db=db)

    assert result is not None and result.converted == len(discovered) and result.refused == 0
    for path in discovered:
        assert "corsia" in validate_skill_text(path.read_text(encoding="utf-8")).metadata, path
    assert projection.read_text(encoding="utf-8") == LEGACY
    assert undiscovered.read_text(encoding="utf-8") == LEGACY


def test_a_symlinked_skill_file_is_refused_not_written_through(tmp_path, monkeypatch):
    from core.skill_key_conversion import convert_legacy_skill_keys

    workspace, _project, db = _workspace(tmp_path, monkeypatch)
    target = _write(tmp_path / "elsewhere" / "SKILL.md", LEGACY)
    link = workspace / "skills" / "linked" / "SKILL.md"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)

    result = convert_legacy_skill_keys(workspace, db=db)

    assert result is not None and result.converted == 0 and result.refused == 1
    assert link.is_symlink() and target.read_text(encoding="utf-8") == LEGACY
    assert MISFILED in validate_skill_text(link.read_text(encoding="utf-8")).errors
