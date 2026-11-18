"""Whether a repo registration's row is well formed and bound through its project's ``source/`` link.

The repo policy loader and the project fence share these rules. The fence runs on every agent
file-tool call, so ``source_bound_paths`` checks bindings without the loader's coalescing and
branch checks: one query, and project roots from rows the caller already holds.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from core.project_context import resolve_project_row_paths


def persisted_repo_path(value: object) -> Path | None:
    """Return a canonical absolute lexical identity without following it."""
    if not isinstance(value, str):
        return None
    path = Path(value)
    if not path.is_absolute() or str(path) != value or os.path.normpath(value) != value:
        return None
    return path


def valid_repo_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = value
    if (
        not name
        or name != name.strip()
        or Path(name).name != name
        or name in {".", ".."}
        or name.startswith(".")
    ):
        return None
    return name


def source_binding_error(planning_root: Path | None, repo_name: str, path: Path) -> str | None:
    """Return why a registration is not bound through its canonical planning root."""
    if planning_root is None:
        return "project planning root is unresolved"
    entry = planning_root / "source" / repo_name
    try:
        current_target = entry.resolve(strict=True)
    except OSError:
        return f"source binding '{repo_name}' is missing"
    if current_target.is_dir() and current_target == path:
        return None
    return f"source binding '{repo_name}' no longer targets the registered checkout"


def source_bound_paths(workspace: Path, db, project_rows: Iterable) -> dict[str, list[Path]]:
    """Each project's registered repo paths whose ``source/`` link still targets them.

    ``project_rows`` carry ``id``, ``name`` and ``path``. A malformed registration, or one in a
    project whose root doesn't resolve, is left out, so it opens nothing.
    """
    roots: dict[str, Path | None] = {}
    for row in project_rows:
        try:
            _name, roots[row["id"]] = resolve_project_row_paths(
                workspace, project_name=row["name"], raw_path=row["path"])
        except (ValueError, OSError):
            roots[row["id"]] = None
    bound: dict[str, list[Path]] = {}
    for row in db.execute("SELECT project_id, repo_name, repo_path FROM project_repo_policies"):
        name, path = valid_repo_name(row["repo_name"]), persisted_repo_path(row["repo_path"])
        root = roots.get(row["project_id"])
        if name and path and source_binding_error(root, name, path) is None:
            bound.setdefault(row["project_id"], []).append(path)
    return bound
