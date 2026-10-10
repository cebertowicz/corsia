"""Tests for check 2 orchestration (check_docs_drift.citation_check)."""

from __future__ import annotations

from check_docs_drift import citation_check as cc


def _minimal_repo(tmp_path):
    (tmp_path / "backend" / "api").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "corsia").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    return tmp_path


def test_cited_python_version_reads_the_readme(tmp_path):
    (tmp_path / "README.md").write_text(
        "| Python 3.11 | CI and the test suite use exactly 3.11. |\n",
        encoding="utf-8",
    )

    assert cc._cited_python_version(tmp_path) == "3.11"


def test_cited_python_version_is_none_without_a_citation(tmp_path):
    (tmp_path / "README.md").write_text("Nothing about Python here.\n", encoding="utf-8")

    assert cc._cited_python_version(tmp_path) is None


def test_check_citations_flags_a_real_ci_pin_mismatch(tmp_path):
    root = _minimal_repo(tmp_path)
    (root / "README.md").write_text(
        "| Python 3.11 | CI and the test suite use exactly 3.11. |\n", encoding="utf-8"
    )
    (root / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  test:\n    steps:\n      - python-version: '3.12'\n", encoding="utf-8"
    )

    result = cc.check_citations(root)

    assert result.python_version_mismatches == ["ci.yml: pins 3.12, docs cite 3.11"]
    assert not result.ok


def test_check_citations_is_clean_when_the_ci_pin_matches_the_citation(tmp_path):
    root = _minimal_repo(tmp_path)
    (root / "README.md").write_text(
        "| Python 3.11 | CI and the test suite use exactly 3.11. |\n", encoding="utf-8"
    )
    (root / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  test:\n    steps:\n      - python-version: '3.11'\n", encoding="utf-8"
    )

    result = cc.check_citations(root)

    assert result.python_version_mismatches == []


def test_check_citations_skips_version_check_without_a_citation(tmp_path):
    """No citation to compare against: no mismatch is fabricated."""
    root = _minimal_repo(tmp_path)
    (root / "README.md").write_text("No Python version mentioned.\n", encoding="utf-8")
    (root / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  test:\n    steps:\n      - python-version: '3.99'\n", encoding="utf-8"
    )

    result = cc.check_citations(root)

    assert result.python_version_mismatches == []
