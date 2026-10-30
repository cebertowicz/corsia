"""FastAPI-free project scope helpers for document operations."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from core.project_context import resolve_project_paths
from infra import config
from infra.project_repo_policy import get_project_repo_policies

MAX_TEXT_BYTES = 1_000_000
UI_THREAD = "ui"


class ProjectNotFound(LookupError):
    """The requested project is absent from the current workspace."""


def resolve_project(project_id: str, db) -> tuple[Path | None, list[tuple[str, Path]]]:
    project_name, planning = resolve_project_paths(
        config.WORKSPACE,
        project_id,
        db=db,
    )
    if project_name is None:
        raise ProjectNotFound("Project not found")
    if planning is None:
        return None, []
    repos: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    policies = get_project_repo_policies(project_id, db=db)
    for name, policy in sorted(
        policies.items(),
        key=lambda item: item[0].lower(),
    ):
        entry = planning / "source" / name
        try:
            target = entry.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if (
            not target.is_dir()
            or target != policy.path
            or target in seen
        ):
            continue
        seen.add(target)
        repos.append((name, target))
    return (planning if planning.is_dir() else None), repos


def scope_roots(project_id: str | None, db) -> tuple[Path | None, list[Path]]:
    """Return the relative-path origin and ordered roots for document access."""
    if project_id:
        planning, repos = resolve_project(project_id, db)
        origin = planning.resolve() if planning else None
        return origin, [path for _, path in repos] + ([origin] if origin else [])
    origin = config.WORKSPACE.resolve()
    return origin, [origin]


def scope_bases(project_id: str | None, db) -> list[Path]:
    return scope_roots(project_id, db)[1]


def path_write_allowed(path: Path, bases: list[Path]) -> bool:
    return any(path == base or base in path.parents for base in bases)


def revision(raw: bytes) -> str:
    """Opaque revision derived solely from the document's current bytes."""
    return hashlib.sha256(raw).hexdigest()


def stat_revision(st: os.stat_result) -> str:
    """Opaque revision of a file too large to hash on every probe, from its size and modification time alone."""
    return f"stat-{st.st_size}-{st.st_mtime_ns}"
