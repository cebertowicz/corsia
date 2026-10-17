"""The one-time startup move of a skill's retired ``corsia:`` block into ``corsia.yaml``."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import get_args

import pytest
from loguru import logger

from core.role_context_sources import RoleName
from core.skill_validation import validate_skill_file
from infra.settings import get_setting
from tests.project_ref_helpers import project_ref, select_workspace

BODY = "\n# Sample\n\nCopy this:\n\n```yaml\n---\ncorsia:\n  handler: {kind: cli}\n---\n```\n\ntrailing   \nñ"
SIDECAR = (
    "handler:\n  kind: instruction\nworkflow:\n  steps:\n    - id: build  # kept comment\n"
    "      required: true\n      check: Build passes\n\n    - {id: ship, after: [build]}\n"
)
BLOCK = "".join(f"  {line}\n" if line.strip() else "\n" for line in SIDECAR.splitlines())
POINTER = "  corsia-data: corsia.yaml\n  corsia-format: '1'\n"
WORKFLOW = f"---\nname: sample\ndescription: A skill\ncorsia:  # moved\n{BLOCK}license: MIT\n---{BODY}"
WORKFLOW_DONE = (
    f"---\nname: sample\ndescription: A skill\nmetadata:\n{POINTER}license: MIT\n---{BODY}"
)
WITH_METADATA = (
    "---\nname: meta\ndescription: Has metadata\nmetadata:\n  # note\n    owner: me\ncorsia:\n"
    "  handler: {kind: cli}\n---\n# Meta\n"
)
WITH_METADATA_DONE = (
    "---\nname: meta\ndescription: Has metadata\nmetadata:\n  # note\n    owner: me\n"
    "    corsia-data: corsia.yaml\n    corsia-format: '1'\n---\n# Meta\n"
)
DEFAULT_ONLY = "---\nname: plain\ndescription: Default handler\ncorsia:\n  handler:\n    kind: instruction\n---\n# Plain\n"
DEFAULT_ONLY_DONE = "---\nname: plain\ndescription: Default handler\n---\n# Plain\n"
LESSON = (
    "---\nname: lesson-skill\ndescription: A lesson\ncorsia:\n  handler:\n    kind: instruction\n"
    "  lesson:\n    state: trial\n---\n# Lesson\n"
)
FLOW_METADATA = "---\nname: flow\ndescription: Flow metadata\nmetadata: {owner: me}\ncorsia:\n  handler: {kind: cli}\n---\n# Flow\n"
INTERRUPTED = (
    "---\nname: half\ndescription: Interrupted\nmetadata:\n" + POINTER + "corsia:\n  handler: {kind: cli}\n---\n# Half\n"
)
INTERRUPTED_DONE = "---\nname: half\ndescription: Interrupted\nmetadata:\n" + POINTER + "---\n# Half\n"


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
        "INSERT INTO projects (id, name, path) VALUES (?, ?, ?)", ("proj-1", "Sample", project_ref("sample")),
    )
    return workspace, project, db


def _convert(workspace, db):
    from core.skill_data_conversion import convert_legacy_skill_data

    return convert_legacy_skill_data(workspace, db=db)


def test_the_block_moves_to_the_sidecar_and_every_other_byte_stays(tmp_path, monkeypatch):
    workspace, _project, db = _workspace(tmp_path, monkeypatch)
    skill = _write(workspace / "skills" / "sample" / "SKILL.md", WORKFLOW)

    result = _convert(workspace, db)

    assert result is not None and result.converted == 1 and result.refused == 0
    assert skill.read_text(encoding="utf-8") == WORKFLOW_DONE
    assert (skill.parent / "corsia.yaml").read_text(encoding="utf-8") == SIDECAR
    resolved = validate_skill_file(skill)
    assert resolved.ok, resolved.errors
    assert resolved.corsia["handler"] == {"kind": "instruction"}
    assert [step["id"] for step in resolved.corsia["workflow"]["steps"]] == ["build", "ship"]


def test_the_pointer_joins_an_existing_block_style_metadata_mapping(tmp_path, monkeypatch):
    workspace, _project, db = _workspace(tmp_path, monkeypatch)
    skill = _write(workspace / "skills" / "meta" / "SKILL.md", WITH_METADATA)

    _convert(workspace, db)

    assert skill.read_text(encoding="utf-8") == WITH_METADATA_DONE
    assert (skill.parent / "corsia.yaml").read_text(encoding="utf-8") == "handler: {kind: cli}\n"
    resolved = validate_skill_file(skill)
    assert resolved.metadata["metadata"]["owner"] == "me" and resolved.corsia == {"handler": {"kind": "cli"}}


def test_a_block_holding_only_the_default_handler_is_dropped_with_no_sidecar(tmp_path, monkeypatch):
    workspace, _project, db = _workspace(tmp_path, monkeypatch)
    skill = _write(workspace / "skills" / "plain" / "SKILL.md", DEFAULT_ONLY)

    result = _convert(workspace, db)

    assert result is not None and result.converted == 1
    assert skill.read_text(encoding="utf-8") == DEFAULT_ONLY_DONE
    assert not (skill.parent / "corsia.yaml").exists()
    assert validate_skill_file(skill).corsia is None


def test_a_lesson_block_stays_exactly_as_it_is_with_no_sidecar(tmp_path, monkeypatch):
    workspace, _project, db = _workspace(tmp_path, monkeypatch)
    skill = _write(workspace / "skills" / "lesson-skill" / "SKILL.md", LESSON)

    result = _convert(workspace, db)

    assert result is not None and result.converted == 0 and result.refused == 0
    assert skill.read_text(encoding="utf-8") == LESSON
    assert not (skill.parent / "corsia.yaml").exists()


def test_an_interrupted_conversion_is_finished_by_removing_the_block(tmp_path, monkeypatch):
    workspace, _project, db = _workspace(tmp_path, monkeypatch)
    skill = _write(workspace / "skills" / "half" / "SKILL.md", INTERRUPTED)
    sidecar = _write(skill.parent / "corsia.yaml", "handler:\n  kind: cli\n")

    result = _convert(workspace, db)

    assert result is not None and result.converted == 1
    assert skill.read_text(encoding="utf-8") == INTERRUPTED_DONE
    assert sidecar.read_text(encoding="utf-8") == "handler:\n  kind: cli\n"
    assert validate_skill_file(skill).corsia == {"handler": {"kind": "cli"}}


def test_an_existing_sidecar_with_the_same_data_and_no_pointer_is_reused(tmp_path, monkeypatch):
    workspace, _project, db = _workspace(tmp_path, monkeypatch)
    skill = _write(workspace / "skills" / "meta" / "SKILL.md", WITH_METADATA)
    sidecar = _write(skill.parent / "corsia.yaml", "handler:\n    kind: cli\n")

    _convert(workspace, db)

    assert skill.read_text(encoding="utf-8") == WITH_METADATA_DONE
    assert sidecar.read_text(encoding="utf-8") == "handler:\n    kind: cli\n"


@pytest.mark.parametrize(
    ("text", "sidecar"),
    [
        pytest.param(FLOW_METADATA, None, id="flow-style-metadata"),
        pytest.param(WITH_METADATA, "handler: {kind: script}\n", id="sidecar-with-different-data"),
        pytest.param(INTERRUPTED, "handler: {kind: script}\n", id="pointer-at-different-data"),
        pytest.param(INTERRUPTED, None, id="pointer-at-no-sidecar"),
        pytest.param("---\nname: a\ndescription: b\ncorsia: {handler: {kind: cli}}\ncorsia: {}\n---\n", None, id="two-blocks"),
    ],
)
def test_an_ambiguous_skill_is_refused_logged_and_left_unchanged(tmp_path, monkeypatch, text, sidecar):
    workspace, _project, db = _workspace(tmp_path, monkeypatch)
    skill = _write(workspace / "skills" / "odd" / "SKILL.md", text)
    if sidecar is not None:
        _write(skill.parent / "corsia.yaml", sidecar)
    lines: list[str] = []
    sink = logger.add(lines.append, format="{level}|{message}", level="WARNING")
    try:
        result = _convert(workspace, db)
    finally:
        logger.remove(sink)

    assert result is not None and result.converted == 0 and result.refused == 1
    assert skill.read_text(encoding="utf-8") == text
    assert any(line.startswith("WARNING|") and str(skill) in line for line in lines), lines
    assert sorted(path.name for path in skill.parent.iterdir()) == sorted(
        ["SKILL.md", *(["corsia.yaml"] if sidecar is not None else [])]
    )


def test_a_symlinked_skill_or_sidecar_is_refused_not_written_through(tmp_path, monkeypatch):
    workspace, _project, db = _workspace(tmp_path, monkeypatch)
    target = _write(tmp_path / "elsewhere" / "SKILL.md", WORKFLOW)
    link = workspace / "skills" / "linked" / "SKILL.md"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    odd = _write(workspace / "skills" / "odd" / "SKILL.md", WITH_METADATA)
    (odd.parent / "corsia.yaml").symlink_to(_write(tmp_path / "other" / "corsia.yaml", "handler: {kind: cli}\n"))

    result = _convert(workspace, db)

    assert result is not None and result.converted == 0 and result.refused == 2
    assert target.read_text(encoding="utf-8") == WORKFLOW and odd.read_text(encoding="utf-8") == WITH_METADATA
    assert not (target.parent / "corsia.yaml").exists()


def test_a_rewrite_that_does_not_read_back_the_same_is_restored(tmp_path, monkeypatch):
    from core import skill_data_conversion

    workspace, _project, db = _workspace(tmp_path, monkeypatch)
    skill = _write(workspace / "skills" / "sample" / "SKILL.md", WORKFLOW)
    monkeypatch.setattr(skill_data_conversion, "_read_back", lambda *_args: False)

    result = _convert(workspace, db)

    assert result is not None and result.converted == 0 and result.refused == 1
    assert skill.read_text(encoding="utf-8") == WORKFLOW
    assert not (skill.parent / "corsia.yaml").exists()


def test_the_pass_runs_once_per_install_and_a_converted_skill_is_stable(tmp_path, monkeypatch):
    from core.skill_data_conversion import COMPLETED_SETTING, rewrite_skill_data

    workspace, _project, db = _workspace(tmp_path, monkeypatch)
    skill = _write(workspace / "skills" / "sample" / "SKILL.md", WORKFLOW)
    first = _convert(workspace, db)
    once, stamp = skill.read_bytes(), skill.stat().st_mtime_ns
    late = _write(workspace / "skills" / "late" / "SKILL.md", WORKFLOW)

    again = _convert(workspace, db)

    assert first is not None and first.converted == 1
    assert get_setting(COMPLETED_SETTING, db=db) is not None and again is None
    assert skill.read_bytes() == once and skill.stat().st_mtime_ns == stamp
    assert late.read_text(encoding="utf-8") == WORKFLOW
    assert rewrite_skill_data(WORKFLOW_DONE, SIDECAR).text == WORKFLOW_DONE


def test_a_pass_that_fails_is_not_recorded_and_runs_again(tmp_path, monkeypatch):
    from core import skill_data_conversion

    workspace, _project, db = _workspace(tmp_path, monkeypatch)
    skill = _write(workspace / "skills" / "sample" / "SKILL.md", WORKFLOW)
    real_skill_files = skill_data_conversion.skill_files

    def fail(_root):
        raise OSError("volume went away")

    monkeypatch.setattr(skill_data_conversion, "skill_files", fail)
    with pytest.raises(OSError, match="volume went away"):
        _convert(workspace, db)
    assert get_setting(skill_data_conversion.COMPLETED_SETTING, db=db) is None

    monkeypatch.setattr(skill_data_conversion, "skill_files", real_skill_files)
    result = _convert(workspace, db)

    assert result is not None and result.converted == 1
    assert skill.read_text(encoding="utf-8") == WORKFLOW_DONE


def test_a_pass_with_an_unresolved_root_is_not_recorded_and_converts_it_later(tmp_path, monkeypatch):
    from core import skill_data_conversion

    workspace, project, db = _workspace(tmp_path, monkeypatch)
    role_skill = _write(project / "context" / "roles" / "agent" / "skills" / "sample" / "SKILL.md", WORKFLOW)
    real_role_directory = skill_data_conversion.role_directory

    def unresolved(scope_root, role):
        raise RuntimeError("role context container cannot be a symlink")

    monkeypatch.setattr(skill_data_conversion, "role_directory", unresolved)
    first = _convert(workspace, db)

    assert first is not None and first.unresolved_roots == len(get_args(RoleName)) * 2 and first.converted == 0
    assert get_setting(skill_data_conversion.COMPLETED_SETTING, db=db) is None
    assert role_skill.read_text(encoding="utf-8") == WORKFLOW

    monkeypatch.setattr(skill_data_conversion, "role_directory", real_role_directory)
    second = _convert(workspace, db)

    assert second is not None and second.unresolved_roots == 0 and second.converted == 1
    assert role_skill.read_text(encoding="utf-8") == WORKFLOW_DONE


def test_every_registry_root_converts_and_the_system_projection_does_not(tmp_path, monkeypatch):
    workspace, project, db = _workspace(tmp_path, monkeypatch)
    discovered = [
        _write(root / rel / "SKILL.md", WORKFLOW)
        for root in (workspace, project)
        for rel in (
            "skills/sample",
            "context/roles/agent/skills/sample",
            "context/roles/conductor/skills/sample",
            ".threads/thread-1/skills/sample",
        )
    ]
    projection = _write(workspace / ".corsia" / "skills" / "sample" / "SKILL.md", WORKFLOW)
    undiscovered = _write(project / "working" / "skills" / "sample" / "SKILL.md", WORKFLOW)

    result = _convert(workspace, db)

    assert result is not None and result.converted == len(discovered) and result.refused == 0
    for path in discovered:
        assert path.read_text(encoding="utf-8") == WORKFLOW_DONE, path
        assert (path.parent / "corsia.yaml").read_text(encoding="utf-8") == SIDECAR
    assert projection.read_text(encoding="utf-8") == WORKFLOW
    assert undiscovered.read_text(encoding="utf-8") == WORKFLOW
