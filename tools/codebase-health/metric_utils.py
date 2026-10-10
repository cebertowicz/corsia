"""Shared helpers for codebase-health metric collectors."""

from __future__ import annotations

import ast
import os
import pathlib
import sys

_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    # Appended, never inserted: this is a library module, so putting scripts/
    # ahead of the stdlib for the life of every importing process would let a
    # colliding module name there silently shadow unrelated imports.
    sys.path.append(str(_SCRIPTS_DIR))

from size_limit_scan import (  # noqa: E402
    FUNCTION_STANDARD,
    STANDARDS,
    count_lines,
    is_test_path,
)

# One executable definition of "too big" lives in scripts/size_limit_scan.py,
# which the blocking ratchet enforces. These names are the local aliases the
# health collectors already read; they must never restate the numbers.
PY_FILE_LIMIT = STANDARDS[".py"]
SVELTE_FILE_LIMIT = STANDARDS[".svelte"]
TS_FILE_LIMIT = STANDARDS[".ts"]
FUNC_LIMIT = FUNCTION_STANDARD
NEST_LIMIT = 4
CHARS_PER_TOKEN = 4
TOKEN_HEADLINE_LIMIT = 8000

EXCLUDE_DIR = (
    "__pycache__",
    "node_modules",
    ".venv",
    ".svelte-kit",
    "/dist/",
    ".worktrees",
    "coverage",
)


def walk_files(roots, exts, extra_skip=()):
    for root in roots:
        for dirpath, _dirnames, filenames in os.walk(root):
            if any(excluded in dirpath for excluded in EXCLUDE_DIR):
                continue
            for filename in filenames:
                if filename.endswith(exts):
                    path = os.path.join(dirpath, filename)
                    if any(skip in path for skip in extra_skip):
                        continue
                    yield path


def is_test(path: str) -> bool:
    """Classify test-only source through the canonical ratchet rule.

    This previously diverged from the ratchet in both directions; the shared
    rule in size_limit_scan is the union of the two, so neither can drift.
    Paths arrive here in OS form from os.walk, so they are normalised to the
    repo-relative POSIX form the rule expects.

    ``is_test_path`` states repo-relative input as a hard precondition, and the
    collectors satisfy it only by convention: they walk relative roots from the
    repository root. An absolute path would break that silently -- a checkout
    or CI workspace directory named ``tests`` anywhere above the repo would
    classify every file under it as test-only and drop it from the metrics.
    So absolute input is relativised here instead of being trusted.
    """
    if os.path.isabs(path):
        path = os.path.relpath(path, os.getcwd())
    return is_test_path(path.replace(os.sep, "/").removeprefix("./"))


def loc(path: str) -> int:
    """Count physical lines, tolerating source that is not valid UTF-8."""
    try:
        with open(path, "rb") as handle:
            return count_lines(handle.read())
    except OSError:
        return 0


def max_nesting(node: ast.AST, current: int = 0) -> int:
    nest_types = (
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.If,
        ast.With,
        ast.AsyncWith,
        ast.Try,
        ast.ExceptHandler,
    )
    if isinstance(node, nest_types):
        current += 1
    return max((current, *(max_nesting(c, current) for c in ast.iter_child_nodes(node))))
