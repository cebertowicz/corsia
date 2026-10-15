"""Workspace conductor role additions keep reaching the CORSIA agent."""

from __future__ import annotations

from pathlib import Path

from infra.corsia_agent_role_files import carry_workspace_conductor_role_files


def _roles(workspace: Path) -> Path:
    return workspace / "context" / "roles"


def _write_conductor_additions(workspace: Path) -> None:
    conductor = _roles(workspace) / "conductor"
    (conductor / "skills" / "audit").mkdir(parents=True)
    (conductor / "10-workspace-conductor.md").write_text("# additions\n", encoding="utf-8")
    (conductor / "skills" / "audit" / "SKILL.md").write_text("# audit\n", encoding="utf-8")


def test_existing_additions_are_copied_and_kept_for_project_conductors(tmp_path: Path) -> None:
    _write_conductor_additions(tmp_path)

    assert carry_workspace_conductor_role_files(tmp_path) is True

    corsia_agent = _roles(tmp_path) / "corsia_agent"
    assert (corsia_agent / "10-workspace-conductor.md").read_text(encoding="utf-8") == "# additions\n"
    assert (corsia_agent / "skills" / "audit" / "SKILL.md").is_file()
    assert (_roles(tmp_path) / "conductor" / "10-workspace-conductor.md").is_file()


def test_existing_target_is_never_overwritten(tmp_path: Path) -> None:
    _write_conductor_additions(tmp_path)
    corsia_agent = _roles(tmp_path) / "corsia_agent"
    corsia_agent.mkdir()

    assert carry_workspace_conductor_role_files(tmp_path) is False
    assert list(corsia_agent.iterdir()) == []


def test_no_additions_writes_nothing(tmp_path: Path) -> None:
    assert carry_workspace_conductor_role_files(tmp_path) is False
    (_roles(tmp_path) / "conductor").mkdir(parents=True)
    assert carry_workspace_conductor_role_files(tmp_path) is False
    assert not (_roles(tmp_path) / "corsia_agent").exists()


def test_symlinked_source_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.md").write_text("x\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    _roles(workspace).mkdir(parents=True)
    (_roles(workspace) / "conductor").symlink_to(elsewhere, target_is_directory=True)

    assert carry_workspace_conductor_role_files(workspace) is False
    assert not (_roles(workspace) / "corsia_agent").exists()
