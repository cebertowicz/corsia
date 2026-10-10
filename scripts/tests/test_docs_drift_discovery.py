"""Tests for shared path/repo discovery helpers (check_docs_drift.discovery)."""

from __future__ import annotations

from check_docs_drift import discovery


def test_resolve_workspace_uses_the_explicit_argument_when_it_exists(tmp_path):
    assert discovery.resolve_workspace(str(tmp_path)) == tmp_path


def test_resolve_workspace_falls_back_to_the_environment_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("CORSIA_WORKSPACE", str(tmp_path))

    assert discovery.resolve_workspace(None) == tmp_path


def test_resolve_workspace_is_none_when_nothing_on_the_chain_exists(tmp_path, monkeypatch):
    monkeypatch.delenv("CORSIA_WORKSPACE", raising=False)
    missing = tmp_path / "does-not-exist"

    assert discovery.resolve_workspace(str(missing)) is None


def test_top_level_names_returns_direct_children_only(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.txt").write_text("x", encoding="utf-8")

    assert discovery.top_level_names(tmp_path) == {"a.txt", "sub"}


def test_repo_root_is_the_grandparent_of_the_package_directory():
    # scripts/check_docs_drift/discovery.py -> parent.parent.parent == repo root
    root = discovery.repo_root()

    assert (root / "scripts" / "check_docs_drift" / "discovery.py").is_file()
