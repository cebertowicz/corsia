"""Tests for `untracked-todo` rule."""

from __future__ import annotations


def test_bare_todo_fires(run_engine):
    """`# TODO: refactor this` with no marker fires."""
    src = (
        "def f():\n"
        "    # TODO: refactor this loop\n"
        "    return 0\n"
    )
    findings = run_engine(
        "backend/foo.py", src, rule_id="untracked-todo"
    )
    assert len(findings) == 1


def test_fixme_fires(run_engine):
    """FIXME without tracking marker fires."""
    src = (
        "def f():\n"
        "    # FIXME this is broken\n"
        "    return 0\n"
    )
    findings = run_engine(
        "backend/foo.py", src, rule_id="untracked-todo"
    )
    assert len(findings) == 1


def test_todo_with_issue_ref_does_not_fire(run_engine):
    """`# TODO(#42): ...` is tracked."""
    src = (
        "def f():\n"
        "    # TODO(#42): refactor this loop\n"
        "    return 0\n"
    )
    findings = run_engine(
        "backend/foo.py", src, rule_id="untracked-todo"
    )
    assert findings == []


def test_todo_with_archived_adr_ref_still_fires(run_engine):
    """Archived ADRs do not own current follow-up work."""
    src = (
        "def f():\n"
        "    # TODO ADR-055: refactor once gate fires\n"
        "    return 0\n"
    )
    findings = run_engine(
        "backend/foo.py", src, rule_id="untracked-todo"
    )
    assert len(findings) == 1


def test_todo_with_deferred_marker_does_not_fire(run_engine):
    """`# TODO (deferred: <reason>)` is tracked."""
    src = (
        "def f():\n"
        "    # TODO (deferred: needs owner sign-off)\n"
        "    return 0\n"
    )
    findings = run_engine(
        "backend/foo.py", src, rule_id="untracked-todo"
    )
    assert findings == []
