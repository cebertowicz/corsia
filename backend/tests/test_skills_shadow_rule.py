"""The System projection shadows local skills and survives projection failure."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from core.skills_registry import (
    SkillResolution,
    local_skill_inventory,
    normalize_skill_scope,
    resolve_effective_skills,
    skill_context_sources,
    skill_scope_root,
)
from infra.system_package import system_package_dir
from tools.sandbox_config import get_allowed_dirs_ro


REAL_PACKAGE = system_package_dir()


def _db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,model,created_at,updated_at) "
        "VALUES ('t1','user',NULL,'native/model','t0','t0')"
    )
    return db


def _write_skill(root: Path, name: str, description: str = "Local skill.") -> None:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n",
        encoding="utf-8",
    )


def _index(tmp_path: Path):
    sources = skill_context_sources(
        thread_id="t1", project_id=None, workspace=tmp_path, db=_db()
    )
    index = next(s for s in sources if s.source_id == "dynamic:skills-index")
    return sources, index


@pytest.fixture
def empty_package(monkeypatch, tmp_path_factory):
    """The real failure: the package location is an empty dir, no manifest, no skills."""
    package = tmp_path_factory.mktemp("empty-package")
    monkeypatch.setattr("infra.system_package._PACKAGE_DIR", package)
    monkeypatch.setattr("core.system_tier._WARNED_UNREADABLE", [])
    return package


@pytest.fixture
def manifest_only_package(empty_package):
    """The manifest is readable but the skill files are not there."""
    shutil.copy(REAL_PACKAGE / "MANIFEST.json", empty_package / "MANIFEST.json")
    return empty_package


def test_package_is_not_in_the_sandbox_allow_list() -> None:
    assert system_package_dir() not in get_allowed_dirs_ro()


def test_system_scope_normalizes_and_root_is_the_projection(tmp_path: Path) -> None:
    assert normalize_skill_scope("system") == "system"
    assert normalize_skill_scope("global") == "workspace"
    assert skill_scope_root(scope="system", workspace=tmp_path, db=None) == tmp_path / ".corsia"


def test_system_rows_use_workspace_projection_paths(tmp_path: Path) -> None:
    rows = local_skill_inventory(scope="system", workspace=tmp_path, db=None)
    assert rows
    assert all(tmp_path / ".corsia" in row.path.parents for row in rows)


def test_system_skill_shadows_workspace_name(tmp_path: Path) -> None:
    _write_skill(tmp_path, "github", "Local override attempt.")

    resolved = resolve_effective_skills(
        thread_id="t1", project_id=None, workspace=tmp_path, db=_db()
    )

    winner = next(row for row in resolved.included if row.name == "github")
    assert winner.scope == "system"
    shadowed = [row for row in resolved.omitted if row.name == "github"]
    assert [row.errors for row in shadowed] == [["shadowed by System"]]


def test_shadowed_copies_render_as_one_index_line(tmp_path: Path) -> None:
    _write_skill(tmp_path, "github", "Override one.")
    _write_skill(tmp_path, "task-discipline", "Override two.")
    _write_skill(tmp_path, "local-only", "Just mine.")

    sources, index = _index(tmp_path)
    lines = (index.prompt_content or "").splitlines()

    shadow_lines = [line for line in lines if "shadowed by System" in line]
    assert shadow_lines == ["- 2 skill names shadowed by System: github, task-discipline"]
    assert any(line.startswith("- local-only:") for line in lines)
    # Per-copy detail stays in the ledger.
    omitted = {s.source_id for s in sources if s.source_id.startswith("skill-omitted")}
    assert len(omitted) == 2
    assert all(s.reason == "shadowed by System" for s in sources if s.source_id in omitted)


def test_system_block_leads_the_index(tmp_path: Path) -> None:
    _write_skill(tmp_path, "local-only", "Just mine.")

    _sources, index = _index(tmp_path)
    lines = [
        line for line in (index.prompt_content or "").splitlines() if line.startswith("- ")
    ]
    in_projection = [str(tmp_path / ".corsia") in line for line in lines]
    first_local = in_projection.index(False)
    assert all(in_projection[:first_local])
    assert not any(in_projection[first_local:])


def test_thread_scope_still_overrides_workspace(tmp_path: Path) -> None:
    _write_skill(tmp_path, "dup", "Workspace version.")
    _write_skill(tmp_path / ".threads" / "t1", "dup", "Thread version.")

    resolved = resolve_effective_skills(
        thread_id="t1", project_id=None, workspace=tmp_path, db=_db()
    )
    winner = next(row for row in resolved.included if row.name == "dup")
    assert winner.scope == "thread"
    omitted = [row for row in resolved.omitted if row.name == "dup"]
    assert omitted and omitted[0].errors == ["overridden by a more specific skill"]


def test_manifest_readable_but_skill_files_missing_says_unavailable_not_shadowed(
    tmp_path: Path, manifest_only_package
) -> None:
    _write_skill(tmp_path, "github", "Local override attempt.")

    resolved = resolve_effective_skills(
        thread_id="t1", project_id=None, workspace=tmp_path, db=_db()
    )
    assert isinstance(resolved, SkillResolution)
    assert resolved.system_unavailable == "System skills unavailable"
    assert not any(row.scope == "system" for row in resolved.included)
    assert not any(row.name == "github" for row in resolved.included)
    assert [row.errors for row in resolved.omitted if row.name == "github"] == [
        ["shadowed by System"]
    ]
    assert local_skill_inventory(scope="system", workspace=tmp_path, db=None) == []

    sources, index = _index(tmp_path)
    errors = [s for s in sources if s.freshness == "error" and s.metadata.get("provider") == "system_skills"]
    assert len(errors) == 1
    assert errors[0].reason == "System skills unavailable"
    # Fixed text: no exception text, path or uuid in the row or the index.
    assert str(tmp_path) not in errors[0].reason
    prompt = index.prompt_content or ""
    assert "- 1 System skill name unavailable, local copies not loaded: github" in prompt
    assert "shadowed by System" not in prompt


def test_empty_package_warns_once_and_local_copies_are_used_behind_the_error_row(
    tmp_path: Path, empty_package, caplog
) -> None:
    _write_skill(tmp_path, "github", "Local copy stays usable.")
    with caplog.at_level("WARNING", logger="core.system_tier"):
        sources, index = _index(tmp_path)
        _index(tmp_path)

    assert "- github: Local copy stays usable." in (index.prompt_content or "")
    assert "shadowed by System" not in (index.prompt_content or "")
    errors = [s for s in sources if s.metadata.get("provider") == "system_skills"]
    assert [e.reason for e in errors] == ["System skills unavailable"]
    assert len([r for r in caplog.records if "System skill manifest" in r.getMessage()]) == 1


def test_protected_shell_argv_does_not_grant_the_package(tmp_path: Path, monkeypatch) -> None:
    """Protected bash receives the workspace projection, never the package."""
    from tools import shell_guard

    monkeypatch.setattr(shell_guard, "_safehouse_executable", lambda: Path("/usr/bin/safehouse"))
    argv = shell_guard.safehouse_argv("true", tmp_path, tmp_path / "profile.sb")

    assert str(system_package_dir().resolve()) not in " ".join(argv)
