"""Deterministic tripwires for the staged outside-PR diff."""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath

ENCODED_BOUND = 256
ENCODED = re.compile(r"[A-Za-z0-9+/=_-]+")
HIDDEN = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")
LOCKFILES = frozenset({"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
                       "Cargo.lock", "Pipfile.lock", "poetry.lock", "uv.lock", "Gemfile.lock",
                       "composer.lock", "go.sum"})
BUILD_FILES = frozenset({"package.json", "setup.py", "setup.cfg", "pyproject.toml", "Makefile"})


def _path_rules(path: str, ci_paths: set[str]) -> set[str]:
    parts = PurePosixPath(path).parts
    name = PurePosixPath(path).name
    hits = set()
    if parts and parts[0] == ".github":
        hits.add("floor.github_dir")
    if (path in ci_paths or name in LOCKFILES or name in BUILD_FILES
            or (name.startswith("requirements") and name.endswith(".txt"))):
        hits.add("floor.ci_file")
    if (parts and parts[0] in {"skills", "context"}) or name in {"AGENTS.md", "CLAUDE.md"} or path.startswith("backend/prompts/"):
        hits.add("floor.agent_instructions")
    return hits


def _encoded_hit(lines: list[str]) -> bool:
    run = 0
    for line in lines:
        if not line.startswith("+") or line.startswith("+++"):
            run = 0
            continue
        text = line[1:].strip()
        matches = list(ENCODED.finditer(text))
        if matches and matches[0].span() == (0, len(text)):
            run += len(text)
        else:
            run = len(matches[-1].group()) if matches and matches[-1].end() == len(text) else 0
        if run >= ENCODED_BOUND or any(len(match.group()) >= ENCODED_BOUND for match in matches):
            return True
    return False


def diff_paths(diff: str) -> set[str]:
    """Extract old and new paths from diff headers, including renames."""
    paths: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        try:
            fields = shlex.split(line)
        except ValueError:
            continue
        paths.update(field[2:] for field in fields[2:4] if field.startswith(("a/", "b/")))
    return paths


def workflow_paths(diff: str, workflows: list[str]) -> set[str]:
    """Changed paths appearing as literal tokens anywhere in workflow text.

    This static tripwire cannot discover indirect or variable-selected execution.
    """
    found = set()
    for path in diff_paths(diff):
        token = re.compile(r"(?:^|[\s\"'])(?:\./)?" + re.escape(path) + r"(?=$|[\s\"'])")
        if any(token.search(text) for text in workflows):
            found.add(path)
    return found


def floor_hits(diff: str, *, ci_paths: set[str] | None = None) -> set[str]:
    """Return matched rule IDs; ci_paths are literal tokens in workflow text."""
    hits: set[str] = set()
    ci = ci_paths or set()
    for path in diff_paths(diff):
        hits.update(_path_rules(path, ci))
    lines = diff.splitlines()
    for line in lines:
        if line.startswith(("Binary files ", "GIT binary patch")):
            hits.add("floor.binary")
        elif (line.startswith(("new file mode 120000", "old mode 120000", "new mode 120000", "deleted file mode 120000"))
              or (line.startswith("index ") and line.endswith(" 120000"))):
            hits.add("floor.symlink")
        if HIDDEN.search(line):
            hits.add("floor.hidden_chars")
    if _encoded_hit(lines):
        hits.add("floor.encoded_blob")
    return hits
