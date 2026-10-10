"""Tests for check 1 (check_docs_drift.starter_kit_check)."""

from __future__ import annotations

import subprocess

import pytest

from check_docs_drift import starter_kit_check as sk


@pytest.fixture(autouse=True)
def _isolated_git_identity(tmp_path, monkeypatch):
    """Isolate git identity resolution from this machine's real config.

    Without this, `check_starter_kit`'s scrub step would resolve whoever's
    real git identity happens to be configured on the machine running the
    test, making the test's outcome machine-dependent.
    """
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(fake_home / "nonexistent-gitconfig"))


def _repo_with_kit(tmp_path):
    repo_root = tmp_path / "repo"
    kit_dir = repo_root / "docs" / "contributing" / "bootstrap" / "starter-kit"
    kit_dir.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "Alice Anderson"], cwd=repo_root, check=True)
    return repo_root, kit_dir


def test_clean_copy_is_reported_clean_after_scrub(tmp_path):
    repo_root, kit_dir = _repo_with_kit(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "context").mkdir(parents=True)
    (workspace / "context" / "x.md").write_text("Alice owns this.", encoding="utf-8")
    (kit_dir / "context").mkdir()
    (kit_dir / "context" / "x.md").write_text("the owner owns this.", encoding="utf-8")

    result = sk.check_starter_kit(repo_root, workspace)

    assert result.clean == ["context/x.md"]
    assert result.drift == {}
    assert result.ok


def test_real_content_change_is_reported_as_drift(tmp_path):
    repo_root, kit_dir = _repo_with_kit(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "context").mkdir(parents=True)
    (workspace / "context" / "x.md").write_text("Alice reviews every PR.", encoding="utf-8")
    (kit_dir / "context").mkdir()
    (kit_dir / "context" / "x.md").write_text("the owner reviews some PRs.", encoding="utf-8")

    result = sk.check_starter_kit(repo_root, workspace)

    assert "context/x.md" in result.drift
    assert not result.ok


def test_kit_native_file_is_not_diffed(tmp_path):
    repo_root, kit_dir = _repo_with_kit(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (kit_dir / "README.md").write_text("Contributor kit readme.", encoding="utf-8")

    result = sk.check_starter_kit(repo_root, workspace)

    assert result.kit_native == ["README.md"]
    assert result.ok


def test_unmapped_kit_path_is_reported(tmp_path):
    repo_root, kit_dir = _repo_with_kit(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (kit_dir / "extras").mkdir()
    (kit_dir / "extras" / "notes.md").write_text("no mapping rule for this.", encoding="utf-8")

    result = sk.check_starter_kit(repo_root, workspace)

    assert result.unmapped == ["extras/notes.md"]
    assert not result.ok


def test_missing_source_is_reported(tmp_path):
    repo_root, kit_dir = _repo_with_kit(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (kit_dir / "context").mkdir()
    (kit_dir / "context" / "gone.md").write_text("copy of a deleted file.", encoding="utf-8")

    result = sk.check_starter_kit(repo_root, workspace)

    assert len(result.missing_source) == 1
    assert result.missing_source[0].startswith("context/gone.md ->")
    assert not result.ok


def test_missing_workspace_fails_by_default():
    """A release gate that cannot verify anything must not report clean."""
    result = sk.check_starter_kit(repo_root=object(), workspace=None)

    assert result.skipped_reason is not None
    assert not result.ok


def test_missing_workspace_can_be_explicitly_allowed():
    """The one legitimate case: an outside contributor's plain clone."""
    result = sk.check_starter_kit(repo_root=object(), workspace=None, require_workspace=False)

    assert result.skipped_reason is not None
    assert result.ok
