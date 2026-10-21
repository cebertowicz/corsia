"""Tests for shared path/repo discovery helpers (check_docs_drift.discovery)."""

from __future__ import annotations

from check_docs_drift import discovery


def test_top_level_names_returns_direct_children_only(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.txt").write_text("x", encoding="utf-8")

    assert discovery.top_level_names(tmp_path) == {"a.txt", "sub"}


def test_repo_root_is_the_grandparent_of_the_package_directory():
    # scripts/check_docs_drift/discovery.py -> parent.parent.parent == repo root
    root = discovery.repo_root()

    assert (root / "scripts" / "check_docs_drift" / "discovery.py").is_file()
