"""Which saved files are guidance history (issue #2272, design §8 "0a Commit on save").

Guidance is ``skills/<name>/SKILL.md``, its ``corsia.yaml`` sidecar and ``skills/<name>/evals/**`` in the
workspace, a project or a thread tray, plus every path the context-source
loaders load (:func:`context_source_paths.classify_context_source` is that
authority). System package content never matches, and neither does a path
whose real file is a symlink, leaves the workspace or is not a regular file.
"""

from __future__ import annotations

import os
from pathlib import Path

from loguru import logger

from core.context_source_paths import (
    _PROJECTS_DIR,
    _THREADS_DIR,
    _absolute,
    classify_context_source,
    configured_source_paths,
)

_TIERS = {"workspace_context": "workspace", "project_context": "project", "thread_context": "agent"}
# Pathspecs that only bound the sweep's status walk; each candidate is still classified.
_SHAPES = ("skills/*/SKILL.md", "skills/*/corsia.yaml", "skills/*/evals/**", "context/*.md", "context/roles/*/*.md")


def _skill_tier(parts: tuple[str, ...]) -> str | None:
    tier = "workspace"
    if len(parts) >= 2 and parts[0] == _PROJECTS_DIR:
        parts, tier = parts[2:], "project"
    if len(parts) >= 2 and parts[0] == _THREADS_DIR:
        parts, tier = parts[2:], "agent"
    if len(parts) < 3 or parts[0] != "skills" or parts[1] in ("", ".", ".."):
        return None
    return tier if parts[2] in (("SKILL.md", "corsia.yaml") if len(parts) == 3 else ("evals",)) else None


def _real_file(absolute: Path, root: Path) -> bool:
    """No symlink below the workspace root: the real path is the lexical one, a regular file or absent."""
    real, real_root = Path(os.path.realpath(absolute)), Path(os.path.realpath(root))
    base = next((base for base in (root, real_root) if absolute.is_relative_to(base)), None)
    if base is None:
        return False
    if real.is_relative_to(real_root) and real.relative_to(real_root) == absolute.relative_to(base):
        if real.is_file() or not os.path.lexists(real):
            return True
    logger.warning("guidance path {} skipped: a symlink, leaving the workspace or not a regular file", absolute)
    return False


def classify_guidance_path(path: str | Path, *, workspace: str | Path, db) -> str | None:
    """The ``Scope`` trailer (e.g. ``skill, agent scope``) of a guidance path, or ``None``."""
    absolute, root = _absolute(path), _absolute(workspace)
    if absolute is None or root is None:
        return None
    if not _real_file(absolute, root):
        return None
    if absolute.is_relative_to(root) and (tier := _skill_tier(absolute.relative_to(root).parts)):
        return f"skill, {tier} scope"
    for change in classify_context_source(absolute, workspace=root, db=db):
        if change.object_kind in _TIERS:
            return f"context, {_TIERS[change.object_kind]} scope"
    return None


def sweep_pathspecs(workspace: Path, db) -> list[str]:
    """Workspace-relative Git pathspecs that can hold guidance, for the sweep."""
    specs = [
        f":(glob){prefix}{shape}"
        for scope in ("", f"{_PROJECTS_DIR}/*/")
        for prefix in (scope, f"{scope}{_THREADS_DIR}/*/")
        for shape in _SHAPES
    ]
    root = workspace.resolve(strict=False)
    for path in sorted(configured_source_paths(workspace, db)):
        if path.resolve(strict=False).is_relative_to(root):
            specs.append(f":(literal){path.resolve(strict=False).relative_to(root).as_posix()}")
    return specs
