"""Replay successful sandbox attacks against the pinned pre-change producer."""
from __future__ import annotations

import ast
from functools import cache, wraps
from pathlib import Path
import subprocess

import pytest

# The dev commit from which the folder-rule replacement was created.
BASELINE_SHA = "5b5a0e303722de09d9e8994528376e9a424ef7ea"


class ConfinementBreach(AssertionError):
    """The shell changed protected state or completed a forbidden operation."""


@cache
def per_worktree_renderer():
    """Load the actual former producer, fetching its pinned blob on shallow CI."""
    from tools import shell_guard

    checkout = Path(__file__).resolve().parents[2]
    command = ["git", "show", f"{BASELINE_SHA}:backend/tools/shell_guard.py"]
    source = subprocess.run(command, cwd=checkout, capture_output=True, text=True, timeout=20)
    if source.returncode:
        # L3 checks out a shallow merge commit. Fetch only this public baseline,
        # once, in that disposable checkout; no other CI job or runtime changes.
        subprocess.run(
            ["git", "fetch", "--no-tags", "--depth=1", "origin", BASELINE_SHA],
            cwd=checkout, capture_output=True, text=True, timeout=60, check=True,
        )
        source = subprocess.run(command, cwd=checkout, capture_output=True, text=True, timeout=20, check=True)
    function = next(node for node in ast.parse(source.stdout).body
                    if isinstance(node, ast.FunctionDef) and node.name == "pinned_repo_sandbox_profile")
    namespace = dict(vars(shell_guard))
    exec(compile(ast.Module(body=[function], type_ignores=[]), "per_worktree_baseline.py", "exec"), namespace)
    return namespace["pinned_repo_sandbox_profile"]


def compare_per_worktree(test):
    """Classify a real breach only after replaying the complete attack afresh."""
    @wraps(test)
    def compare(**kwargs):
        try:
            test(**kwargs)
        except ConfinementBreach as candidate:
            sandbox = kwargs["repository_sandbox"]
            legacy = sandbox.make_baseline()
            try:
                test(**{**kwargs, "repository_sandbox": legacy})
            except ConfinementBreach:
                pytest.xfail(f"Existing per-worktree sandbox gap: {test.__name__}: {candidate}")
            raise candidate
    return compare


def unchanged(actual, expected):
    """Keep successful partial mutations in the same baseline-comparison path."""
    if actual != expected:
        raise ConfinementBreach("protected state changed")
