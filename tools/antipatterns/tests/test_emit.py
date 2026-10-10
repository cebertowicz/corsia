"""Unit tests for the dev_tool.antipatterns_invoked product event emit.

Verifies that ``cli.main()`` calls the emit helper with the expected
payload shape on every exit path (no-files-in-scope + post-engine-run),
and that the emit failure mode doesn't poison the CLI's pass/fail
signal.

The tests stub out the emit helper at the module level — we're not
testing the backend wrapper here (that's `backend/tests/test_product_events.py`),
just the dev-tool emit-point contract.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from antipatterns import cli as ap_cli


@pytest.fixture
def captured_emits(monkeypatch):
    """Capture every ``_emit_invocation_event`` call as a kwargs dict."""
    captured: list[dict] = []

    def fake_emit(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(ap_cli, "_emit_invocation_event", fake_emit)
    return captured


def test_emit_fires_on_empty_scope(tmp_path, captured_emits, monkeypatch):
    """No files in scope still emits — the invocation count is the signal."""
    monkeypatch.chdir(tmp_path)
    # --paths with a path that doesn't exist won't get classified as a file,
    # but engine.run will still produce zero findings. Use a clean tmp_path
    # with --paths to force the empty-scope path.
    rc = ap_cli.main(["--paths", "nonexistent.py", "--repo-root", str(tmp_path)])
    # Even though no findings, the emit must have happened.
    assert len(captured_emits) == 1
    payload = captured_emits[0]
    assert payload["files_scanned"] in (0, 1)  # depends on engine path classification
    assert payload["findings_count"] == 0
    assert payload["severity_counts"] == {"error": 0, "warn": 0}
    assert payload["mode"] in {"manual", "ci", "pre-commit"}
    assert rc == 0


def test_emit_fires_on_clean_scan(tmp_path, captured_emits, monkeypatch):
    """A clean Python file → emit fires with files_scanned=1, findings=0."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "clean.py"
    target.write_text('"""Clean module."""\n\n\ndef hello():\n    """Say hi."""\n    return "hi"\n')

    rc = ap_cli.main(["--paths", str(target), "--repo-root", str(tmp_path)])

    assert rc == 0
    assert len(captured_emits) == 1
    payload = captured_emits[0]
    assert payload["files_scanned"] == 1
    assert payload["findings_count"] == 0
    assert payload["severity_counts"] == {"error": 0, "warn": 0}


def test_emit_includes_severity_counts_on_findings(tmp_path, captured_emits, monkeypatch):
    """A file with a known anti-pattern → emit captures severity buckets."""
    monkeypatch.chdir(tmp_path)
    # ``except Exception: pass`` is the silent-error-swallow positive fixture.
    target = tmp_path / "bad.py"
    target.write_text(
        '"""Module docstring."""\n\n\n'
        "def f():\n"
        '    """Do thing."""\n'
        "    try:\n"
        "        return 1\n"
        "    except Exception:\n"
        "        pass\n"
    )

    rc = ap_cli.main(
        ["--paths", str(target), "--repo-root", str(tmp_path), "--severity", "error"]
    )

    assert rc == 1  # blocking finding at default error severity
    assert len(captured_emits) == 1
    payload = captured_emits[0]
    assert payload["files_scanned"] == 1
    assert payload["findings_count"] >= 1
    assert payload["severity_counts"]["error"] >= 1


def test_emit_does_not_block_when_disabled(tmp_path, captured_emits, monkeypatch):
    """Setting CORSIA_ANALYTICS_DISABLE=1 short-circuits the real emit.

    The fixture replaces ``_emit_invocation_event`` with a capturing stub
    so the env-var short-circuit only takes effect inside the real
    helper. We still expect the cli to call the stub (the stub doesn't
    read the env var); the real helper's env-var short-circuit is
    covered by the unit test of the real helper below.
    """
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "clean.py"
    target.write_text('"""ok."""\n')
    rc = ap_cli.main(["--paths", str(target), "--repo-root", str(tmp_path)])
    assert rc == 0
    # stub was called regardless (env-var short-circuit is inside the
    # real helper, not the cli's call site)
    assert len(captured_emits) == 1


def test_real_helper_skips_when_disabled(monkeypatch):
    """The real ``_emit_invocation_event`` returns cleanly when disabled."""
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")
    # Should not raise and should not import infra.product_events.
    ap_cli._emit_invocation_event(
        mode="manual",
        files_scanned=5,
        findings_count=2,
        severity_counts={"error": 1, "warn": 1},
    )


def test_detect_mode_pre_commit(monkeypatch):
    """PRE_COMMIT=1 → mode == pre-commit, regardless of args."""
    import argparse
    monkeypatch.setenv("PRE_COMMIT", "1")
    args = argparse.Namespace(staged=False)
    assert ap_cli._detect_mode(args) == "pre-commit"


def test_detect_mode_ci(monkeypatch):
    """GITHUB_ACTIONS=true or CI=true → mode == ci."""
    import argparse
    monkeypatch.delenv("PRE_COMMIT", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    args = argparse.Namespace(staged=False)
    assert ap_cli._detect_mode(args) == "ci"


def test_detect_mode_manual_default(monkeypatch):
    """No env + no --staged → mode == manual."""
    import argparse
    monkeypatch.delenv("PRE_COMMIT", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    args = argparse.Namespace(staged=False)
    assert ap_cli._detect_mode(args) == "manual"


def test_detect_mode_staged_implies_pre_commit(monkeypatch):
    """--staged without env hints → pre-commit (it's the canonical surface)."""
    import argparse
    monkeypatch.delenv("PRE_COMMIT", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    args = argparse.Namespace(staged=True)
    assert ap_cli._detect_mode(args) == "pre-commit"
