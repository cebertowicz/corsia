"""The pinned pre-change producer, for parity checks where behavior must not change.

No attack is excused by it: there is no known-gap list, so every breach fails.
"""
from __future__ import annotations

import ast
from functools import cache
from pathlib import Path
import subprocess

# The dev commit from which the folder-rule replacement was created.
BASELINE_SHA = "5b5a0e303722de09d9e8994528376e9a424ef7ea"


class ConfinementBreach(AssertionError):
    """The shell changed protected state or completed a forbidden operation; always a failure."""


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
    from tools.sandbox_config import list_repo_worktrees

    names = {"_verified_worktrees", "pinned_repo_sandbox_profile"}
    functions = [node for node in ast.parse(source.stdout).body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in functions} == names
    namespace = {**vars(shell_guard), "list_repo_worktrees": list_repo_worktrees}
    exec(compile(ast.Module(body=functions, type_ignores=[]), "per_worktree_baseline.py", "exec"), namespace)
    return namespace["pinned_repo_sandbox_profile"]


def unchanged(actual, expected):
    """Keep successful partial mutations in the same baseline-comparison path."""
    if actual != expected:
        raise ConfinementBreach("protected state changed")
